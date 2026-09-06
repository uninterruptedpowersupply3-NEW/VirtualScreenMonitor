# vddmon server runtime overrides & hooks
# Hot-reloaded live on damage sweeps without server restart.

DANGEROUS_VKS = {0x5B, 0x5C, 0x5D}  # VK_LWIN, VK_RWIN, VK_APPS

def on_keysym(ks, down):
    """Return True to suppress forwarding this key to the OS."""
    # Suppress Windows keys (XK_Super_L, XK_Super_R) and Menu/Apps key (XK_Menu)
    if ks in (0xFFEB, 0xFFEC, 0xFF67):
        return True
    return False

config = {
    # "priority": "idle",
}

