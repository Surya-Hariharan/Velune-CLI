import json
import os
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from velune.providers.crypto import decrypt_credentials, encrypt_credentials
from velune.providers.keystore import CredentialManager, save_key, get_key, delete_key, has_key

@pytest.fixture
def mock_config_dir(tmp_path):
    with patch("velune.providers.keystore.user_config_dir", return_value=str(tmp_path)):
        # Reset the singleton instance for each test
        CredentialManager._instance = None
        from velune.providers.keystore import _manager
        _manager._init()
        
        yield tmp_path
        
        CredentialManager._instance = None
        _manager._init()

@pytest.fixture
def mock_keyring():
    with patch("velune.providers.crypto.get_or_create_master_key") as mock_get_key:
        import base64
        # Provide a stable 32-byte fake key (32 bytes of 'A')
        mock_get_key.return_value = base64.b64decode("QUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUFBQUE=")
        yield mock_get_key

def test_save_and_get_key(mock_config_dir, mock_keyring):
    save_key("openai", "sk-test-123")
    assert has_key("openai")
    assert get_key("openai") == "sk-test-123"

def test_multiple_providers_merge(mock_config_dir, mock_keyring):
    save_key("openai", "sk-123")
    save_key("anthropic", "sk-ant-456")
    
    # Both should exist
    assert get_key("openai") == "sk-123"
    assert get_key("anthropic") == "sk-ant-456"

def test_restart_cli(mock_config_dir, mock_keyring):
    """Test that restarting the CLI (re-instantiating CredentialManager) retains keys."""
    save_key("groq", "gsk_test")
    assert get_key("groq") == "gsk_test"
    
    # Simulate restart
    CredentialManager._instance = None
    from velune.providers.keystore import _manager
    _manager._init()
    
    assert has_key("groq")
    assert get_key("groq") == "gsk_test"

def test_delete_key(mock_config_dir, mock_keyring):
    save_key("cohere", "coh-123")
    assert has_key("cohere")
    
    delete_key("cohere")
    assert not has_key("cohere")
    assert get_key("cohere") is None

def test_corrupted_config_recovery(mock_config_dir, mock_keyring):
    """If credentials.json is corrupted, it should restore from backup or start fresh."""
    save_key("openai", "sk-good")
    save_key("openai", "sk-good")  # Second save creates the backup of the first state
    
    # Manually corrupt the file
    creds_file = mock_config_dir / "credentials.json"
    creds_file.write_bytes(b"garbage data not encrypted properly")
    
    # Simulate restart
    CredentialManager._instance = None
    from velune.providers.keystore import _manager
    _manager._init()
    
    # Since we have a backup created during atomic write, it should recover
    assert get_key("openai") == "sk-good"

def test_interrupted_write(mock_config_dir, mock_keyring):
    """An interrupted write shouldn't destroy the existing file."""
    save_key("openai", "sk-first")
    
    mgr = CredentialManager()
    
    def crash_replace(self_obj, target):
        raise OSError("Simulated crash during replace")
        
    with patch("pathlib.Path.replace", autospec=True, side_effect=crash_replace):
        try:
            save_key("anthropic", "sk-crash")
        except OSError:
            pass
            
    # Simulate restart
    CredentialManager._instance = None
    from velune.providers.keystore import _manager
    _manager._init()
    
    # The original file should still be intact and contain openai
    assert get_key("openai") == "sk-first"
    # The anthropic key didn't save because it crashed
    assert get_key("anthropic") is None

def test_overwrite_existing_key(mock_config_dir, mock_keyring):
    save_key("gemini", "gemini-old")
    assert get_key("gemini") == "gemini-old"
    
    save_key("gemini", "gemini-new")
    assert get_key("gemini") == "gemini-new"

def test_save_empty_key_does_not_crash(mock_config_dir, mock_keyring):
    save_key("ollama", "")
    assert get_key("ollama") == ""

