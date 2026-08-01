from doupool.desktop import find_free_port


def test_find_free_port_returns_loopback_port():
    port = find_free_port()
    assert isinstance(port, int)
    assert 0 < port < 65536
