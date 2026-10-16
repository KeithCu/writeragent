import subprocess

def run(cmd):
    subprocess.run(cmd, shell=True, check=True)

run("git commit -m 'Test: Add Packet I image generation mock LLM tests'")
