---
name: review
description: Review code for bugs, security issues, and improvements
---

Review the provided code or repository. Focus on:

1. **Bugs** — logic errors, edge cases, off-by-one errors
2. **Security** — injection, auth issues, exposed secrets, OWASP top 10
3. **Performance** — unnecessary allocations, N+1 queries, blocking calls
4. **Style** — naming, structure, consistency with project conventions

Output a concise list of findings with severity (critical/warning/info) and suggested fixes. If no issues found, say so briefly.
