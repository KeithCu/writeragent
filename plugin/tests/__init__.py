import os

# Dynamically point package search path to the repository root tests directory
__path__ = [os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "tests")]
