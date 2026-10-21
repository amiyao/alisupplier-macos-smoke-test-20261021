"""Seal small connection handoffs for the local session key; never print plaintext."""
import base64
import json
import pathlib
import subprocess
import sys

def seal(public_key, value):
    raw = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("ascii")
    if len(raw) > 318:
        raise ValueError("RSA-3072 OAEP-SHA256 handoff exceeds 318 bytes")
    result = subprocess.run(
        ["openssl", "pkeyutl", "-encrypt", "-pubin", "-inkey", str(public_key),
         "-pkeyopt", "rsa_padding_mode:oaep", "-pkeyopt", "rsa_oaep_md:sha256",
         "-pkeyopt", "rsa_mgf1_md:sha256"],
        input=raw, capture_output=True, timeout=15)
    if result.returncode:
        raise RuntimeError("Connection handoff encryption failed")
    return {"version": 1, "algorithm": "RSA-OAEP-SHA256",
            "ciphertext": base64.b64encode(result.stdout).decode("ascii")}

if __name__ == "__main__":
    # Input and output are local files, never command-line passwords.
    public_key, input_path, output_path = map(pathlib.Path, sys.argv[1:4])
    packet = seal(public_key, json.loads(input_path.read_text(encoding="utf-8")))
    output_path.write_text(json.dumps(packet), encoding="utf-8")


