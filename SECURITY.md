# Security policy

Security fixes are applied to the latest commit on the default branch.

Please use GitHub private vulnerability reporting for security issues. Do not
open a public issue containing credentials, private resource data, proprietary
game content, or an exploitable proof of concept.

Resource bytecode is loaded in a restricted Lua environment without filesystem,
network, process, package, or debug APIs. Even so, process only resources you
are authorized to inspect and prefer an isolated operating-system account for
untrusted inputs.
