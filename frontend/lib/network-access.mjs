import net from "node:net";

export function normalizeIPv4(address) {
  if (typeof address !== "string") {
    return null;
  }
  const candidate = address.startsWith("::ffff:")
    ? address.slice("::ffff:".length)
    : address;
  return net.isIPv4(candidate) ? candidate : null;
}

function ipv4ToNumber(address) {
  const normalized = normalizeIPv4(address);
  if (!normalized) {
    throw new Error(`Invalid IPv4 address: ${address}`);
  }
  return normalized.split(".").reduce(
    (value, octet) => ((value << 8) | Number(octet)) >>> 0,
    0,
  );
}

export function parseIPv4Cidr(cidr) {
  if (typeof cidr !== "string") {
    throw new Error("IPv4 CIDR must be a string");
  }
  const [networkAddress, prefixText, extra] = cidr.trim().split("/");
  const prefixLength = Number(prefixText);
  if (
    extra !== undefined ||
    !normalizeIPv4(networkAddress) ||
    prefixText === undefined ||
    prefixText === "" ||
    !Number.isInteger(prefixLength) ||
    prefixLength < 0 ||
    prefixLength > 32
  ) {
    throw new Error(`Invalid IPv4 CIDR: ${cidr}`);
  }

  const mask = prefixLength === 0
    ? 0
    : (0xffffffff << (32 - prefixLength)) >>> 0;
  return {
    cidr: `${networkAddress}/${prefixLength}`,
    mask,
    network: ipv4ToNumber(networkAddress) & mask,
  };
}

export function isIPv4Allowed(address, parsedCidrs) {
  const normalized = normalizeIPv4(address);
  if (!normalized || !Array.isArray(parsedCidrs)) {
    return false;
  }
  const numericAddress = ipv4ToNumber(normalized);
  return parsedCidrs.some(
    ({ mask, network }) => (numericAddress & mask) === network,
  );
}
