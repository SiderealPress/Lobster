# slack-connector skill — src package
#
# Modules:
#   account_mode: Token detection, validation, and account-type resolution
#   ingress_logger: Raw Slack event logging (JSONL, with dedup)
#   log_store: Read-only query interface over log files
#   channel_config: Per-channel routing and behavior config
#   user_permissions: Per-user allow/deny lists
#   onboarding: Setup instructions and config writing for bot/person paths
