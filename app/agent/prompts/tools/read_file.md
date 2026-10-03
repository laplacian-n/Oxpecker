TOOL CARD: read_file

Reads a text file from inside the agent's workspace only — a path outside the workspace, or one
touching a known credential location (SSH keys, cloud credentials, .netrc, etc.), is rejected
regardless of how it's phrased. Large files are capped; a truncated read should be reported as
partial, not treated as the complete file content.
