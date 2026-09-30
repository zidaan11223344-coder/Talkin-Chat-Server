"""Print a Fernet key for STATE_ENCRYPTION_KEY; run once and store securely."""
from state import CredentialVault

if __name__ == "__main__":
    print(CredentialVault.generate())
