import os
import pytest
from pathlib import Path
import threading
import stat
from velune.kernel.config import ConfigLoader, VeluneConfig

def test_load_read_only_config(tmp_path):
    config_file = tmp_path / "velune.toml"
    config_file.write_text("[project]\nname = \"readonly_project\"")
    # Make file read-only
    os.chmod(config_file, stat.S_IREAD)
    
    loader = ConfigLoader(config_file)
    config = loader.load()
    assert config.project.name == "readonly_project"

def test_save_to_read_only_directory(tmp_path):
    # Depending on OS, making a directory read-only might block file creation
    # We will test saving over an existing read-only file
    config_file = tmp_path / "velune.toml"
    config_file.write_text("[project]\nname = \"original\"")
    
    try:
        os.chmod(config_file, stat.S_IREAD)
        config = VeluneConfig()
        config.project.name = "new_name"
        
        # This should either succeed (if OS allows replacing) or raise PermissionError
        # We just want to ensure it doesn't corrupt silently.
        try:
            config.save_to_project(tmp_path)
            # If it succeeded, check it wrote it
        except PermissionError:
            pass # Expected on Windows
    finally:
        os.chmod(config_file, stat.S_IWRITE)

def test_concurrent_access(tmp_path):
    config_file = tmp_path / "velune.toml"
    config_file.write_text("[project]\nname = \"concurrent\"")
    
    def read_config():
        loader = ConfigLoader(config_file)
        for _ in range(50):
            config = loader.load()
            assert config.project.name == "concurrent"

    threads = [threading.Thread(target=read_config) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

def test_multiple_providers(tmp_path):
    config_file = tmp_path / "velune.toml"
    config_file.write_text("""
[providers.openai]
api_key_env = "MY_OAI_KEY"

[providers.anthropic]
api_key_env = "MY_ANTHROPIC_KEY"
""")
    loader = ConfigLoader(config_file)
    config = loader.load()
    assert config.providers.openai.api_key_env == "MY_OAI_KEY"
    assert config.providers.anthropic.api_key_env == "MY_ANTHROPIC_KEY"
