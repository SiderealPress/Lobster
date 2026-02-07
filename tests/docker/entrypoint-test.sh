#!/bin/bash
# Lobster Test Entrypoint
#
# This script initializes the test environment and runs tests.

set -e

echo "=========================================="
echo "  Lobster Test Runner"
echo "=========================================="

# Navigate to lobster directory
cd /home/testuser/lobster

# Activate virtual environment
if [ -f ".venv/bin/activate" ]; then
    source .venv/bin/activate
fi

# Ensure test dependencies are installed
pip install -q -r tests/requirements-test.txt 2>/dev/null || true

# Initialize test directories
mkdir -p /home/testuser/messages/{inbox,outbox,processed,config,audio,task-outputs}
mkdir -p /home/testuser/lobster-workspace/logs
mkdir -p /home/testuser/lobster/scheduled-tasks/{tasks,logs}

# Initialize required JSON files
echo '{"tasks": [], "next_id": 1}' > /home/testuser/messages/tasks.json
echo '{"jobs": {}}' > /home/testuser/lobster/scheduled-tasks/jobs.json

# Parse arguments
TEST_TYPE="${1:-all}"
shift 2>/dev/null || true
EXTRA_ARGS="$@"

echo ""
echo "Running tests: $TEST_TYPE"
echo "Extra arguments: $EXTRA_ARGS"
echo ""

case "$TEST_TYPE" in
    unit)
        pytest tests/unit -v --tb=short $EXTRA_ARGS
        ;;
    integration)
        pytest tests/integration -v --tb=short -m "not docker" $EXTRA_ARGS
        ;;
    stress)
        pytest tests/stress -v --tb=short --timeout=300 $EXTRA_ARGS
        ;;
    install)
        pytest tests/integration/test_installation.py -v --tb=short $EXTRA_ARGS
        ;;
    mcp)
        pytest tests/unit/test_mcp_server -v --tb=short $EXTRA_ARGS
        ;;
    bot)
        pytest tests/unit/test_bot -v --tb=short $EXTRA_ARGS
        ;;
    daemon)
        pytest tests/unit/test_daemon -v --tb=short $EXTRA_ARGS
        ;;
    cli)
        pytest tests/unit/test_cli -v --tb=short $EXTRA_ARGS
        ;;
    all)
        pytest tests/ -v --tb=short -m "not docker" $EXTRA_ARGS
        ;;
    *)
        echo "Unknown test type: $TEST_TYPE"
        echo ""
        echo "Available types:"
        echo "  unit        - Run unit tests"
        echo "  integration - Run integration tests"
        echo "  stress      - Run stress tests"
        echo "  install     - Run installation tests"
        echo "  mcp         - Run MCP server tests"
        echo "  bot         - Run bot tests"
        echo "  daemon      - Run daemon tests"
        echo "  cli         - Run CLI tests"
        echo "  all         - Run all tests"
        exit 1
        ;;
esac

echo ""
echo "=========================================="
echo "  Tests Complete"
echo "=========================================="
