import platform
import socket
import getpass

print("=" * 40)
print("Anthony's System Information")
print("=" * 40)

print(f"Computer Name:  {socket.gethostname()}")
print(f"User: {getpass.getuser()}")
print(f"Operating System: {platform.system()}")
print(f"OS Version: {platform.version()}")
print(f"Machine: {platform.machine()}")
print(f"Processor: {platform.processor()}")

print("=" *40)
print("First Python Project Complete!")
print("=" * 40)
