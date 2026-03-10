import os

os.system("git restore tests/test_streaming.py")
os.system("git restore tests/test_config_service.py")
os.system("git restore tests/test_constants.py")

# Also delete any .coverage artifact
os.system("rm -f .coverage")
