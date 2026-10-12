# Out of scope, and why

Things the TVs can do that this server deliberately doesn't expose. Each would be a design change,
not a small addition: it widens what a model can do to a TV.

| Capability | Why not |
| --- | --- |
| Raw SSAP requests, or luna:// calls | One generic call reaches everything the TV exposes, including internal settings services: the same risk class as `adb shell` on the Shield, which mcp-server-shieldtv also refuses. |
| Picture and sound settings (picture mode, backlight, Filmmaker Mode) | Writing them needs `WRITE_SETTINGS`, which webOS 26 refuses an unsigned-manifest key (HA core #172703), or the "luna alert" hack, which is an internal API by the back door. Reading the picture mode might be worth adding later, read-only. |
| Toasts / on-screen messages | Arbitrary text on the TV's screen; decided against with the other free-text tools. |
| Opening a browser or a URL, YouTube by video id | Lets the model point the TV at anything. |
| Text input, mouse pointer | Free text, and unbounded navigation. |
| Channels and Live TV | Not how these TVs are used (they sit on the receiver's input); the keys for it are left out too. |
| Reboot | No safe use case that outweighs interrupting whatever is on; the Shield has one only because its Bluetooth remotes need it. |
| Calibration (bscpylgtv's LUT upload) | Specialist work with its own tools; also removed by LG on webOS 26. |
| LG IP Control (TCP 9761, keycode + AES) | A second protocol to maintain for little gain over SSAP; worth a look only if picture settings become a goal. |
| Wake-on-LAN for arbitrary MACs | Only the MACs `pair` saved for a configured TV. |
