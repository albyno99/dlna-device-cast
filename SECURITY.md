# Security Policy

## Reporting a vulnerability

Please report security issues privately by email to **security@albertosesia.it**.
Do not open a public issue for a vulnerability until it has been fixed.

Include, where possible:

- a description of the issue and its impact;
- steps to reproduce, or a proof of concept;
- the version or commit you tested and your macOS and TV/receiver details.

You will receive an acknowledgement within 5 working days. Confirmed issues are fixed in the
`main` branch and credited in the release notes, unless you prefer to remain anonymous.

## Supported versions

Only the latest commit on `main` receives security fixes.

## Scope and threat model

Device Cast is designed for a trusted local network.

- The screen stream is **not encrypted or authenticated**. Anyone able to observe traffic on
  the local network can see the cast screen.
- The low-latency server answers only the TV it was started for, loopback and addresses given
  with `--allow`. Started without `--tv` or `--allow`, it answers any address on the network.
- The DLNA server binds to the interface that reaches the TV and answers only the TV.

In scope:

- ways for a device on the network other than the selected receiver to obtain the screen
  content;
- remote code execution, file disclosure or denial of service in the sender (`sender/`,
  `app/`) triggered by network input (SSDP, UPnP descriptions, HTTP, WebSocket);
- issues in the Tizen receiver app that allow another host to inject content.

Out of scope:

- attacks that require an attacker already able to sniff or spoof traffic on the same LAN,
  given that the stream is unencrypted by design;
- vulnerabilities in third-party software (macOS, ffmpeg, DeskPad, Tizen, the TV firmware).
