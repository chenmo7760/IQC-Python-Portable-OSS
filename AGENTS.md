# Collaboration rules

- The coordinating conversation assigns work, performs read-only review, and delivers results.
- Delegate implementation, testing and packaging to worker agents. A delegated worker
  completes its assigned work directly without recursively delegating for this rule.
- Use a capable coding model with high reasoning effort or above for import, validation,
  packaging, troubleshooting and review work.
- Keep sample data synthetic. Never publish credentials, personnel records, site paths,
  production identifiers, deployment mappings, binaries or database exports.
- Keep preview and execution separate. Missing dependencies/configuration must fail
  visibly. Do not run real interfaces or database writes as part of offline tests.
