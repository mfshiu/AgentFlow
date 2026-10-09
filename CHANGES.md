## Unreleased

- Agent configuration and EmptyBroker options are logged at DEBUG with nested
  credential fields redacted. INFO startup messages no longer contain full
  configuration. Live broker options and authentication values are unchanged.
- Empty, Redis and ROS publish diagnostics now contain only topic and payload
  length at DEBUG, never payload content. Log-consuming scripts must not depend
  on the former config/options/payload representations.
- Applications can use `agentflow.logging.redact_config` for their own config
  diagnostics; downstream loaders must also stop logging unredacted configuration.
