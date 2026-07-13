# Performance report

Turbo remained interactive during API, GUI, and the 120-second soak. The soak captured 19/19 healthy samples and 4/4 successful non-empty chats. Mono-perf direct probes took about 7 seconds but returned degenerate content. Mono crossed a 20-minute startup deadline, one 16-token direct probe took 47.25 seconds, and provider logs showed 0.1-0.4 tok/s. These 31B observations meet the prompt's practical-unusability threshold for a performance finding.
