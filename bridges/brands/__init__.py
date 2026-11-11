"""One module per headphone protocol. Each defines HEADSET, a core.Headset.

The names are what `cornerstone-bridge <brand>` takes and what the `bridge`
field of a row in lib/backends.js names. tools/check makes sure every name
here has a module, a test file and at least one pin.
"""
NAMES = (
    "airoha",
    "bose",
    "earfun",
    "huawei",
    "jbl",
    "moto",
    "nothing",
    "oppo",
    "redmi",
    "samsung",
    "sony",
    "soundcore",
    "tozo",
    "xiaomi",
)
