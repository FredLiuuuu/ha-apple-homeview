# Apple HomeView (AirPlay) for Home Assistant

A Home Assistant custom integration for Apple HomeView (model `HomeAccessory17,1`).
It pairs with HomeView over AirPlay, shows what HomeView is playing, and lets
Home Assistant add and remove AirPlay 2 speakers (HomePods and third-party
AirPlay 2 receivers) in the group HomeView is playing to.

> **Stop-gap.** This exists because the pyatv changes HomeView needs are not in
> a pyatv release yet (see [Credits](#credits)). When pyatv ships them and the
> core `apple_tv` integration supports HomeView and grouping, this integration
> should be retired in favour of core.

## What it does

- **Pairing** with an AirPlay PIN shown on HomeView, from the HA UI.
- **`media_player` entity** with now playing (title, artist, album, artwork,
  app, position), play/pause/stop/next/previous/seek and volume.
- **AirPlay 2 group control**:
  - `media_player.join` adds speakers to HomeView's AirPlay group;
  - `homeview.remove_members` removes them;
  - `homeview.set_outputs` sets the exact list of outputs.
  - The entity's `group_members` and `airplay_outputs` attributes show the
    current group.
- **Blueprint** (`blueprints/automation/airplay_rejoin.yaml`) that re-adds a
  receiver to the group after it reboots or reconnects.

## Requirements

- Home Assistant 2026.11 or newer (tested on 2026.11 with pyatv 0.18.0).
- HomeView and Home Assistant on the same network, with mDNS/Bonjour working
  between them (or enter HomeView's address during setup).
- pyatv ≥ 0.18.0 (Home Assistant already ships it).

## Installation (HACS custom repository)

1. In HACS, open the menu (⋮) → **Custom repositories**.
2. Add `https://github.com/FredLiuuuu/ha-apple-homeview` with type **Integration**.
3. Install **Apple HomeView (AirPlay)** and restart Home Assistant.

Manual install: copy `custom_components/homeview/` into your HA
`config/custom_components/` folder and restart.

## Pairing

1. **Settings → Devices & services → Add integration → Apple HomeView (AirPlay)**.
2. Leave **Host** empty to search the network, or enter HomeView's address.
   Choose **Pair with PIN**.
3. HomeView shows a 4-digit PIN. Type it into the form.
4. The integration checks that the remote-control channel works, then creates
   the device and its `media_player` entity.

**Import from file** is an alternative for people who already paired with
pyatv (e.g. `atvremote --protocol airplay pair`). Point it at a text file that
contains only the AirPlay credentials string. The file is read once; the
credentials are then stored in the config entry, like any other integration
secret. Delete the file afterwards and never commit it anywhere.

### Map speakers

For `media_player.join` and `homeview.remove_members` to work, tell the
integration which AirPlay output belongs to which `media_player` entity:
**Settings → Devices & services → Apple HomeView → Configure**. Each output
HomeView currently reports is listed; pick its entity. Outputs not listed right
now can be added as extra lines: `media_player.my_speaker = <output id>`
(output ids appear in the entity's `airplay_outputs` attribute).

## Services

| Service | Target | Data | What it does |
| --- | --- | --- | --- |
| `media_player.join` | HomeView entity | `group_members`: speaker entities | Adds the speakers to HomeView's AirPlay group. Speakers already in the group are skipped. |
| `homeview.remove_members` | HomeView entity | `group_members`: speaker entities | Removes the speakers from HomeView's group. |
| `homeview.set_outputs` | HomeView entity | `output_ids`: list of AirPlay output ids | Sets HomeView's outputs to exactly this list. |

Example:

```yaml
action: media_player.join
target:
  entity_id: media_player.homeview
data:
  group_members:
    - media_player.kitchen_homepod
    - media_player.living_room_receiver
```

`media_player.unjoin` on the HomeView entity is refused on purpose (it is
unclear whether it should end the whole group); use `homeview.remove_members`.

## Blueprint: re-join a receiver after it reboots

Some AirPlay 2 receivers (for example ESP32-based ones) drop out of the group
when they reboot or lose power, and HomeView does not add them back. The
blueprint [`airplay_rejoin.yaml`](blueprints/automation/airplay_rejoin.yaml)
does that:

- It tracks whether the receiver is in the leader's group, in an
  `input_boolean`, and the time it left, in an `input_datetime` (date and
  time). **Create both helpers first**; a blueprint cannot create them.
- When the receiver comes back (an entity goes from `unavailable` to
  available, or an optional uptime sensor drops), and the leader is playing,
  and the receiver was in the group or left less than the grace period ago:
  - if the leader still lists the stale receiver, it waits for the leader to
    drop it, and only removes it itself as a fallback (removing a playing
    receiver can briefly interrupt the whole group);
  - then calls `media_player.join` until the receiver plays, up to N tries;
  - and writes "ok" or "FAILED" to the logbook.

Known limit: if you remove the receiver on purpose and it happens to reboot
within the grace period, it is added back.

Import it with **Settings → Automations & scenes → Blueprints → Import
blueprint** and this file's GitHub URL.

## Command-line alternative

pyatv's own `atvremote` can list and change HomeView's outputs without Home
Assistant, once you have AirPlay credentials:

```sh
atvremote --id <homeview id> --airplay-credentials <credentials> output_devices
atvremote --id <homeview id> --airplay-credentials <credentials> add_output_devices=<output id>
atvremote --id <homeview id> --airplay-credentials <credentials> remove_output_devices=<output id>
```

(Until pyatv #2952 is released you need a pyatv build with that PR for
`atvremote` to reach HomeView's remote-control channel.)

## Limits

- **HomeView only.** Discovery looks for `HomeAccessory*` models. Apple TVs and
  HomePods are handled by the core `apple_tv` integration.
- **Backported pyatv fixes.** Pairing patches a private pyatv header table, but
  only while this integration pairs, and skips the patch if pyatv changes. A
  future pyatv release may still need an update here.
- **Group state lags.** HomeView does not reliably push group changes,
  especially for third-party receivers. The integration re-reads the output
  list on a short-lived second connection every 60 s and after each change,
  and shows a change you made straight away for up to 90 s until HomeView
  confirms it.
- **Removing a speaker HomeView added, via a HomePod, does not work.** Change
  HomeView's group through HomeView (this integration).
- **Removing a playing receiver can interrupt the group** for a moment; adding
  one back has not been heard to.
- Volume is HomeView's group volume; per-speaker volume is not exposed.
- No `play_media`/source selection: start playback on HomeView itself.
- Tested with one HomeView, HomePods, and an ESP32 AirPlay 2 receiver. Other
  setups may behave differently; please open an issue.

## Credits

- [pyatv](https://github.com/postlund/pyatv) by @postlund (MIT), which does all
  the protocol work.
- The HomeView fixes backported in `custom_components/homeview/pyatv_compat.py`
  come from open pyatv pull requests:
  - [postlund/pyatv#2894](https://github.com/postlund/pyatv/pull/2894)
    "Fix AirPlay PIN pairing on tvOS 27" by @JaviSoto;
  - [postlund/pyatv#2952](https://github.com/postlund/pyatv/pull/2952)
    "Support Apple HomeView remote control and device model" by @zackwag.

  They will be removed once pyatv releases them.

## License

Apache License 2.0. See [LICENSE](LICENSE).

## Disclaimer

This is an independent, unofficial project. It is not affiliated with,
endorsed by, or sponsored by Apple Inc. Apple, AirPlay, HomePod and Apple TV
are trademarks of Apple Inc., registered in the U.S. and other countries and
regions; HomeView is an Apple product name. These names are used here only to
describe what this integration works with.
