# Third-party components bundled in Salvage.app

Salvage itself is shipped under its own licence (see the project root). The
macOS app bundle also embeds the following external command-line tools and
their shared libraries, unmodified, as built by Homebrew. Their licences
govern those binaries specifically.

## PhotoRec / TestDisk

- **Binary bundled:** `photorec`
- **Licence:** GNU General Public License v2 (GPLv2)
- **Project:** TestDisk & PhotoRec, by Christophe GRENIER
- **Source / homepage:** https://www.cgsecurity.org/wiki/TestDisk_Download
- Source code for the exact version bundled is available from the project
  homepage above; no modifications were made to the binary beyond copying it
  into this app bundle.

## libimobiledevice

- **Binaries bundled:** `idevice_id`, `ideviceinfo`, `idevicebackup2`, `afcclient`
- **Licence:** GNU Lesser General Public License v2.1 (LGPLv2.1)
- **Project:** libimobiledevice
- **Source / homepage:** https://libimobiledevice.org
- These tools also link against `libimobiledevice`, `libimobiledevice-glue`,
  `libusbmuxd`, and `libplist` (all libimobiledevice.org projects, LGPLv2.1),
  plus OpenSSL's `libssl`/`libcrypto` (Apache License 2.0), all bundled
  alongside them in `Contents/Frameworks/`. Because these are dynamically
  linked LGPL libraries used unmodified and without static linking into
  Salvage's own code, Salvage's own source may remain under its own licence;
  the libraries' own source is available from their respective project pages.

Nothing under this section was authored by Assembly Growth / the Salvage
project; it is redistributed here only for the app to function without
requiring a separate Homebrew install of TestDisk and libimobiledevice.
