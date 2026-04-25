#!/bin/bash

# Create the reports directory
mkdir -p scripts/coverage_reports

echo "Running pytest with coverage for the framework directory..."

# Run coverage and output to text inside the coverage_reports directory
uv run pytest --cov=plugin/framework --cov-report=term-missing > scripts/coverage_reports/coverage_summary.txt

echo "Coverage report generated at scripts/coverage_reports/coverage_summary.txt"
