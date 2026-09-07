# Review policy

Review the candidate against every acceptance criterion in the approved specification,
the approved plan, implementation deviations, and the controller's verification evidence.

- Correctness: check edge cases, error handling, and regressions introduced by the diff.
- Security: check input handling, authorization, secrets, and unintended external effects.
- Requirements: identify missing behavior, scope expansion, or unsupported product choices.
- Tests: require useful evidence for changed behavior. Flag deleted, disabled, or weakened
  tests unless the approved scope justifies the change.

An important finding must describe a concrete defect with a file, line, and supporting evidence.
Optional polish is a nit. Follow the factory review schema for the verdict and finding limits.
