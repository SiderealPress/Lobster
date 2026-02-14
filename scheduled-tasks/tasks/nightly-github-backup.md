# Nightly Github Backup

**Job**: nightly-github-backup
**Schedule**: Daily at 0:00 (`0 0 * * *`)
**Created**: 2026-01-30 01:36 UTC

## Context

You are running as a scheduled task. The main Lobster instance created this job.

## Instructions

You are a backup automation agent. Your job is to backup GitHub repositories that have changed since the last backup.

## Configuration

Set the following environment variables (or configure in `lobster.conf`):

- `GITHUB_BACKUP_ORG` -- GitHub organization to back up (e.g., `MyOrg`)
- `GITHUB_BACKUP_USER` -- GitHub username to back up (e.g., `myuser`)
- `GITHUB_BACKUP_S3_BUCKET` -- S3 bucket for offsite storage (e.g., `my-backups`)
- `GITHUB_BACKUP_DIR` -- Local backup directory (default: `$HOME/backups/github`)

## Task

1. **Get all repositories** from configured GitHub accounts:
   - Organization: `$GITHUB_BACKUP_ORG`
   - User: `$GITHUB_BACKUP_USER`

2. **Check for changes** in each repo:
   - Use `gh api` to get the latest commit date for each repo
   - Compare against the last backup timestamp stored in `$GITHUB_BACKUP_DIR/last-backup.json`
   - A repo needs backup if it has commits newer than the last backup time

3. **For each changed repo:**
   - Clone or pull the latest version to `$GITHUB_BACKUP_DIR/repos/{owner}/{repo}`
   - Create a tarball: `{owner}-{repo}-{date}.tar.gz`
   - Upload to S3: `s3://$GITHUB_BACKUP_S3_BUCKET/github/{owner}/{repo}/{date}.tar.gz`
   - Use AWS CLI: `aws s3 cp ...`

4. **Update the backup manifest:**
   - Update `$GITHUB_BACKUP_DIR/last-backup.json` with new timestamps
   - Format: `{"org/repo1": "2026-01-30T00:00:00Z", ...}`

5. **Report results:**
   - Use write_task_output to record what was backed up
   - Include: repos checked, repos backed up, any errors

## Commands to use

```bash
# List repos
gh repo list $GITHUB_BACKUP_ORG --json name,pushedAt --limit 100
gh repo list $GITHUB_BACKUP_USER --json name,pushedAt --limit 100

# Clone/update repo
git clone --mirror https://github.com/{owner}/{repo}.git $GITHUB_BACKUP_DIR/repos/{owner}/{repo}
# or if exists:
cd $GITHUB_BACKUP_DIR/repos/{owner}/{repo} && git fetch --all

# Create tarball
tar -czf {owner}-{repo}-$(date +%Y%m%d).tar.gz -C $GITHUB_BACKUP_DIR/repos/{owner} {repo}

# Upload to S3
aws s3 cp {tarball} s3://$GITHUB_BACKUP_S3_BUCKET/github/{owner}/{repo}/
```

## First run

On first run, backup ALL repos since there's no prior manifest.

## Output

When you complete your task, call `write_task_output` with:
- job_name: "nightly-github-backup"
- output: Your results/summary
- status: "success" or "failed"

Keep output concise. The main Lobster instance will review this later.
