# webos (LG TVs): cheat sheet for the home-theater Skill

Drop into the Skill as `devices/webos.md`. Server: `mcp-server-webos`, tools prefixed by the
connector name (e.g. `webos`).

## The TVs

| Room | TV | Notes |
| --- | --- | --- |
| Family Room | 77" G1 (webOS 6) | sound to the receiver over eARC: volume_owner "external" |
| Office | 65" C5 (webOS 25/26) | sound to the receiver over eARC: volume_owner "external" |
| Bedroom | 65" C7 (webOS 3.5) | no screen-off; power state inferred |

(Expected values; confirm with `get_status` and update after the hardware pass.)

## Tools, and what each confirms

| Tool | Use it to | Confirms |
| --- | --- | --- |
| `get_status(tv)` | check first, every time | power (on, screen_off, standby, off), app or input in front, volume_owner, capabilities |
| `set_power(state, tv)` | on (Wake-on-LAN) / off; `tv="all"` for off | on: that the TV answered within 20 s, else a warning |
| `select_source(source, tv)` | an app ("netflix") or an input ("Onkyo") | that it is in front |
| `list_sources(tv)` | see what select_source accepts | |
| `send_key(key, repeat, tv)` | navigate (UP/DOWN/LEFT/RIGHT/ENTER/BACK/HOME) | sent only; you can't see the screen |
| `media_control(action, tv)` | play/pause/stop/rewind/fast_forward | sent only |
| `set_screen(state, tv)` | picture off for music, keep sound | the power state changes |
| `set_volume` / `set_mute` | only when volume_owner is "tv" | |

## House rules this server already enforces

- With several TVs, every action needs `tv`; it never guesses.
- Volume on a TV whose sound goes to the receiver is refused: use the Onkyo server.
- A TV that needs pairing says so (`needs_pairing`); don't retry, tell the user to run `pair`.

## For the Skill to decide (not the server)

- In a room whose hub activity (Harmony, Sofabaton) owns power and input, let the activity do
  those; use this server for apps, keys and status. Don't mix a hub's IR power toggle with
  `set_power` in the same workflow.
- "off" in get_status means not answering: off, or off the network. `set_power on` is the fix
  for the first; doctor for the second.
- now_playing has the app and play state only; webOS doesn't report titles.
