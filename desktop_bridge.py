"""Ephemeral SSH transport: one public key, one loopback target, no shell or files."""
import asyncio
import pathlib
import sys
import asyncssh

PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIBqGYDti8Igcd2xUaIx5I7CiYNjYmWYYzNx4hx5AiLj/"

class DesktopOnlyServer(asyncssh.SSHServer):
    def __init__(self, key=PUBLIC_KEY):
        self.key = asyncssh.import_public_key(key)
        self.user = None

    def begin_auth(self, username):
        self.user = username
        return True

    def public_key_auth_supported(self):
        return True

    def validate_public_key(self, username, key):
        return username == "runner" and key == self.key

    def password_auth_supported(self):
        return False

    def kbdint_auth_supported(self):
        return False

    def connection_requested(self, dest_host, dest_port, orig_host, orig_port):
        return self.user == "runner" and dest_host == "127.0.0.1" and dest_port == 6080

    def session_requested(self):
        return False

    def server_requested(self, listen_host, listen_port):
        return False

    def unix_connection_requested(self, dest_path):
        return False

    def unix_server_requested(self, listen_path):
        return False

async def serve(hostkey):
    async with asyncssh.create_server(DesktopOnlyServer, "127.0.0.1", 22022,
            server_host_keys=[hostkey], login_timeout=20, agent_forwarding=False,
            x11_forwarding=False, allow_scp=False) as server:
        await server.wait_closed()

if __name__ == "__main__":
    asyncio.run(serve(sys.argv[1]))

