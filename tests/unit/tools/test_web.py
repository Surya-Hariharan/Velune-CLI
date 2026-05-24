import pytest
from velune.tools.web.validator import validate_url
from velune.tools.web.fetch import WebFetch

def test_validate_url():
    # Valid HTTPS
    valid, err = validate_url("https://example.com")
    assert valid is True
    assert err is None
    
    # Invalid Scheme
    valid, err = validate_url("file:///etc/passwd")
    assert valid is False
    assert "scheme" in err
    
    # HTTP when HTTPS is required
    valid, err = validate_url("http://example.com")
    assert valid is False
    assert "HTTP not allowed" in err
    
    # Localhost / Loopback
    valid, err = validate_url("https://localhost:11434")
    assert valid is False
    assert "localhost" in err
    
    # Private IP range
    valid, err = validate_url("https://192.168.1.1")
    assert valid is False
    
    # Link Local / Reserved / Metadata endpoints
    valid, err = validate_url("https://169.254.169.254")
    assert valid is False
    assert "metadata" in err or "private/reserved" in err

@pytest.mark.asyncio
async def test_web_fetch_execution():
    fetch = WebFetch()
    
    # Executing malicious URL raises ValueError
    with pytest.raises(ValueError, match="URL validation failed"):
        await fetch.execute("file:///etc/passwd")
        
    with pytest.raises(ValueError, match="URL validation failed"):
        await fetch.execute("https://localhost:11434")
