# Cornerstone Phones

Headphone battery and noise-cancelling control in the Omarchy bar, part of the
Cornerstone desktop by Kush Beaver Productionz.

- One icon per connected set, filling with its battery; the panel shows left,
  right and case, and switches Off / ANC / Ambient where the headphones allow.
- Talks to each brand through its own bridge (`*-bridge`), including
  Motorola Moto Buds+ (`moto-bridge`, added for Cornerstone). Google Fast Pair
  and BlueZ give battery on anything else.
- IPC: `omarchy-shell cornerstone-phones <method>`.

Built from Omaphones by Jacek Becela (MIT, see LICENSE). Protocol notes for
each brand are in PROTOCOL.md and docs/captures.
