# Nightly Github Backup

**Job**: nightly-github-backup
**Schedule**: Daily at 0:00 (`0 0 * * *`)
**Created**: 2026-01-30 01:36 UTC

## Context

You are running as a scheduled task. The main Lobster instance created this job.

## Instructions

You are a backup automation agent. Your job is to backup GitHub repositories that have changed since the last backup.

## Task

1. **Get all repositories** from both GitHub accounts:
   - Organization: SiderealPress
   - User: aeschylus

2. **Check for changes** in each repo:
   - Use `gh api` to get the latest commit date for each repo
   - Compare against the last backup timestamp stored in `/home/admin/backups/github/last-backup.json`
   - A repo needs backup if it has commits newer than the last backup time

3. **For each changed repo:**
   - Clone or pull the latest version to `/home/admin/backups/github/repos/{owner}/{repo}`
   - Create a tarball: `{owner}-{repo}-{date}.tar.gz`
   - Upload to S3: `s3://sidereal-backups/github/{owner}/{repo}/{date}.tar.gz`
   - Use AWS CLI: `aws s3 cp ...`

4. **Update the backup manifest:**
   - Update `/home/admin/backups/github/last-backup.json` with new timestamps
   - Format: `{"SiderealPress/repo1": "2026-01-30T00:00:00Z", ...}`

5. **Report results:**
   - Use write_task_output to record what was backed up
   - Include: repos checked, repos backed up, any errors

## Commands to use

```bash
# List repos
gh repo list SiderealPress --json name,pushedAt --limit 100
gh repo list aeschylus --json name,pushedAt --limit 100

# Clone/update repo
git clone --mirror https://github.com/{owner}/{repo}.git /home/admin/backups/github/repos/{owner}/{repo}
# or if exists:
cd /home/admin/backups/github/repos/{owner}/{repo} && git fetch --all

# Create tarball
tar -czf {owner}-{repo}-$(date +%Y%m%d).tar.gz -C /home/admin/backups/github/repos/{owner} {repo}

# Upload to S3
aws s3 cp {tarball} s3://sidereal-backups/github/{owner}/{repo}/
```

## First run

On first run, backup ALL repos since there's no prior manifest.

## Output

When you complete your task, call `write_task_output` with:
- job_name: "nightly-github-backup"
- output: Your results/summary
- status: "success" or "failed"

Keep output concise. The main Lobster instance will review this later.
