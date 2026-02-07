# Test Job

**Job**: test-job
**Schedule**: Every 5 minutes (`*/5 * * * *`)
**Created**: 2026-01-24 09:30 UTC

## Context

You are running as a scheduled task. The main Lobster instance created this job.

## Instructions

This is a test job. Just say 'Test job ran successfully!' and write the output.

## Output

When you complete your task, call `write_task_output` with:
- job_name: "test-job"
- output: Your results/summary
- status: "success" or "failed"

Keep output concise. The main Lobster instance will review this later.
