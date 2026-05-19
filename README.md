# JOCK Strap

JOCK Strap is a PITS extension that connects your local inventory to a SHOWER community server for real-time sync.

## Features

- Discord OAuth via SHOWER (no local Discord tokens)
- Real-time inventory sync via WebSocket
- Bidirectional: local changes sync to community, community changes sync back
- Quantity merge on duplicate entries
- Connection status indicator
- Auto-sync option

## Installation

1. Download `JOCKstrap_v1.0.zip` from the [Releases page](https://github.com/zee2ex2/SC-PITS-JOCKstrap-Extension/releases)
2. In PITS, go to **Settings → Manage Extensions → Install New Extension**
3. Select the ZIP file
4. The extension will appear in the extensions list

## Configuration

1. Open PITS **Settings**
2. Find the **JOCK Strap** section
3. Enter your SHOWER server URL
4. Click **Save** then **Login with Discord**
5. Once connected, enable **Auto-sync inventory changes to community**

## Requirements

- PITS v0.3.0 or later
- SHOWER server with WebSocket enabled
- Discord account with access to the guild

## License

MIT
