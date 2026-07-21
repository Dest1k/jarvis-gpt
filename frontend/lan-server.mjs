import http from "node:http";
import net from "node:net";

import next from "next";

import {
  isIPv4Allowed,
  parseIPv4Cidr,
} from "./lib/network-access.mjs";

const loopbackAddress = "127.0.0.1";
const lanAddress = (process.env.JARVIS_UI_LAN_BIND_ADDRESS || "").trim();
const allowedCidrText = (process.env.JARVIS_UI_ALLOWED_CIDRS || "").trim();
const port = Number(process.env.PORT || "3000");

if (!net.isIPv4(lanAddress) || lanAddress.startsWith("127.")) {
  throw new Error("JARVIS_UI_LAN_BIND_ADDRESS must be a non-loopback IPv4 address");
}
if (!Number.isInteger(port) || port < 1 || port > 65535) {
  throw new Error("PORT must be an integer from 1 to 65535");
}

const allowedCidrs = allowedCidrText
  .split(",")
  .map((value) => value.trim())
  .filter(Boolean)
  .map(parseIPv4Cidr);
if (allowedCidrs.length === 0) {
  throw new Error("JARVIS_UI_ALLOWED_CIDRS must contain at least one IPv4 CIDR");
}
if (!isIPv4Allowed(loopbackAddress, allowedCidrs)) {
  throw new Error("JARVIS_UI_ALLOWED_CIDRS must allow the IPv4 loopback interface");
}
if (!isIPv4Allowed(lanAddress, allowedCidrs)) {
  throw new Error("The LAN bind address must belong to JARVIS_UI_ALLOWED_CIDRS");
}

const app = next({ dev: false, hostname: lanAddress, port });
await app.prepare();
const handle = app.getRequestHandler();

function createServer(bindAddress) {
  const server = http.createServer((request, response) => {
    if (!isIPv4Allowed(request.socket.remoteAddress, allowedCidrs)) {
      response.writeHead(403, {
        "Cache-Control": "no-store",
        Connection: "close",
        "Content-Type": "application/json; charset=utf-8",
      });
      response.end('{"detail":"Client network is not allowed"}');
      return;
    }

    Promise.resolve(handle(request, response)).catch((error) => {
      console.error("Jarvis UI request failed", error);
      if (!response.headersSent) {
        response.writeHead(500, {
          "Cache-Control": "no-store",
          "Content-Type": "application/json; charset=utf-8",
        });
      }
      response.end('{"detail":"Internal server error"}');
    });
  });
  server.headersTimeout = 30_000;
  server.requestTimeout = 120_000;
  server.keepAliveTimeout = 5_000;
  server.maxHeadersCount = 100;
  server.on("clientError", (_error, socket) => {
    if (socket.writable) {
      socket.end("HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n");
    }
  });
  return { bindAddress, server };
}

const listeners = [createServer(loopbackAddress), createServer(lanAddress)];

async function listen({ bindAddress, server }) {
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen({ host: bindAddress, port }, () => {
      server.removeListener("error", reject);
      resolve();
    });
  });
  console.log(`Jarvis UI listening on http://${bindAddress}:${port}`);
}

try {
  for (const listener of listeners) {
    await listen(listener);
  }
} catch (error) {
  await Promise.allSettled(
    listeners.map(({ server }) => new Promise((resolve) => server.close(resolve))),
  );
  await app.close();
  throw error;
}

let shuttingDown = false;
async function shutdown(signal) {
  if (shuttingDown) {
    return;
  }
  shuttingDown = true;
  console.log(`Jarvis UI received ${signal}; stopping`);
  await Promise.allSettled(
    listeners.map(({ server }) => new Promise((resolve) => server.close(resolve))),
  );
  await app.close();
}

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => {
    shutdown(signal)
      .then(() => process.exit(0))
      .catch((error) => {
        console.error("Jarvis UI shutdown failed", error);
        process.exit(1);
      });
  });
}
