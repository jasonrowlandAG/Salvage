# Salvage — Licensing & Compliance Assessment

**Prepared:** 2026-09-18, ahead of public release.
**Status:** Technical/licensing analysis with primary-source citations
(license texts fetched directly from each project's own repository — not
from memory). **This is not legal advice from a licensed attorney.**

**Updated 2026-09-18:** the one HIGH RISK item this document carried — x265
(GPLv2) loading in-process via the pillow-heif wheel — has been resolved by
removing the component, not by settling the legal question: the dependency
is now `pi-heif`, upstream's decode-only wheel, which bundles no encoder and
documents itself as LGPLv3. No item in this document now turns on an
unsettled legal question. The rest is about as settled as third-party
license compliance gets, and you can act on it directly.

The companion file `packaging/THIRD_PARTY.md` is the actual notices document
that ships with the app (full license texts, written offer of source). This
file is the reasoning and the business-decision framing behind it.

---

## What you may and may not do

**You may:**
- Distribute Salvage for free, publicly, as-is, including every third-party
  component currently bundled (PhotoRec, the libimobiledevice family,
  OpenSSL, PySide6/Qt, Pillow, pycryptodome, and pi-heif's bundled
  libheif/libde265) — provided you ship the license texts, notices, and
  written source offer now completed in `packaging/THIRD_PARTY.md`, and add
  the Licences/About screen specified in `docs/release-checklist.md`.
- Sell Salvage. No component's license prohibits charging money for it —
  GPL, LGPL, CPL, IPL, Apache-2.0, MIT, and BSD all explicitly permit
  commercial redistribution; GPL in particular is often misread as
  "must be free," but the FSF has always meant free as in freedom, not
  free as in price.
- Keep Salvage's own code (everything under `salvage/`) MIT-licensed,
  including when sold. None of the bundled components' licenses require
  Salvage's own original source to be published or relicensed. The one
  contested exception this document used to carry (x265) has been removed
  from the bundle — see the resolved finding below.

**You may not:**
- Strip, omit, or fail to pass along the license texts, copyright notices,
  and (for the GPL-licensed parts) the written source offer — required
  whether Salvage is free or paid.
- Add restrictions on top of what a GPL/LGPL/CPL/IPL component's own license
  allows a recipient to do with *that specific component* (GPLv2 §6, similar
  clauses elsewhere) — e.g. you can't tell a customer "you may not extract
  or redistribute the copy of PhotoRec inside this app," because PhotoRec's
  own license already grants them that right and yours can't take it away.
  This doesn't extend to Salvage's own MIT code, which you fully control.
- Claim Salvage is warranted or that you're the author of PhotoRec, the
  Sleuth Kit, libimobiledevice, Qt, or any other bundled component — the
  notices in `packaging/THIRD_PARTY.md` exist specifically so this
  distinction is unambiguous.
- Ship a build that claims "no bundled GPL software" or otherwise
  misrepresents what's inside. PhotoRec is still GPLv2. The general trap is
  worth remembering even though the specific instance is fixed: a Python
  package can be permissive for its own code (pillow-heif is BSD-3-Clause)
  while the prebuilt native libraries in its *wheel* are not.

---

## The one thing to get a real lawyer to review before a paid launch

### RESOLVED (2026-09-18) — x265 is no longer in the bundle

This document previously carried a HIGH RISK finding here: x265 (GPLv2, an
HEVC **encoder**) arrived inside pillow-heif's prebuilt wheel, loaded
in-process via Python's C-extension mechanism, and therefore raised a
genuinely contested question about whether the *combined, running*
Salvage.app was a GPL-covered work. That finding is now closed by removing
the component rather than by resolving the legal theory — the outcome this
document recommended.

**What changed:** the `pillow-heif` dependency was replaced with `pi-heif`,
upstream's own decode-only wheel of the same project, which bundles only
libheif and libde265 (both LGPLv3) and no encoder. Salvage never encodes
HEIC — it only decodes, for photo thumbnails and previews — so nothing was
lost. pi-heif's own `LICENSES_bundled.txt` states *"License for 'pi-heif'
binary wheels: LGPLv3, due to base library licenses."*

**Verified on a real build, not just in the dependency list:**
- `find dist/Salvage.app -iname '*x265*'` → no hits.
- `otool -L` across every bundled Mach-O → no `x265` references anywhere
  (bundled `libheif` now links only `libde265` and system libraries).
- `vmmap` on the running app → only `_pi_heif…so`, `libheif`, `libde265`.
- HEIC preview still works: a real `.heic` renders as both a grid thumbnail
  and a full preview in the built app, with correct dimensions read through
  the decoder.

**Why the cheaper mitigation was rejected:** simply deleting
`libx265.216.dylib` from the built bundle does not work. `libheif` hard-links
it (`@loader_path/libx265.216.dylib`), so with the file gone the whole
extension fails to load — `ImportError: Library not loaded:
@loader_path/libx265.216.dylib` — and HEIC decoding dies with it (silently,
since the import sits behind a `try/except ImportError`). This was tested in
an isolated venv before being ruled out. Rebuilding libheif from source with
`-DWITH_X265=OFF` would also have worked but is strictly more effort than
using the decode-only wheel upstream already publishes.

**What remains GPL in the bundle:** PhotoRec/TestDisk only, invoked as a
clean subprocess. That is the mainstream, well-settled "mere aggregation"
pattern — see the combined-work section below. There is no longer any
in-process GPL library in Salvage.

### 1. The Sleuth Kit tools your own code depends on aren't in the installer

Not itself a hard legal question (CPL-1.0/IPL-1.0 are lenient, see below) —
but `docs/release-checklist.md` and `packaging/THIRD_PARTY.md` both flag it
as a functional release blocker, and closing it pulls in two more licenses
(libewf, and afflib's non-standard advertising clause) that need a decision,
not just documentation. Worth a lawyer's five minutes only for the afflib
clause if you decide to bundle it; the CPL/IPL obligations themselves are
routine.

---

## Per-component table

| Component | Version | License | Obligation | What we must do |
|---|---|---|---|---|
| PhotoRec / TestDisk | 7.2 | GPL-2.0-or-later | §3: source or written offer (≥3yr); §6: no added restrictions | Done — text + written offer in `THIRD_PARTY.md` |
| pi-heif wheel: libheif | 1.23.0 | LGPL-3.0 | §4(d): dynamic linking + notice | Done — text + notice |
| pi-heif wheel: libde265 | 1.1.0 | LGPL-3.0 | §4(d): dynamic linking + notice | Done — text + notice |
| ~~pillow-heif wheel: x265~~ | ~~soname 216 (~4.2)~~ | ~~GPL-2.0~~ | — | **Removed from the bundle 2026-09-18** — replaced pillow-heif with the decode-only pi-heif wheel; see the resolved finding above |
| libimobiledevice, libplist, libusbmuxd, libimobiledevice-glue, libtatsu | 1.4.0 / 2.7.0 / 2.1.1 / 1.3.2 / 1.0.5 | LGPL-2.1-or-later | §6(b): dynamic linking + notice | Done — text + notice |
| OpenSSL | 3.6.x | Apache-2.0 | §4: retain notices | Done — text + attribution |
| PySide6 / Qt 6 (+ bundled FFmpeg) | 6.11.2 | LGPL-3.0-only (elected option) | §4(d): dynamic linking + notice; relies on Hardened Runtime staying off, see below | Done — text + notice; **re-check if/when Hardened Runtime is enabled for notarization** |
| Pillow | 12.3.0 | MIT-CMU | Notice only | Done |
| pycryptodome | 3.23.0 | Public domain + BSD-2-Clause | Notice only | Done |
| The Sleuth Kit: fls, fsstat, mmls | 4.15.0 | CPL-1.0 | §3: disclose source availability | Not bundled — documented for when it is |
| The Sleuth Kit: icat | 4.15.0 | IPL-1.0 | §3: disclose source availability | Not bundled — documented for when it is |
| libewf | 20140816 | LGPL-3.0-or-later | §4(d): dynamic linking + notice | Not bundled — documented for when it is |
| afflib | 3.7.22 | BSD-4-Clause (non-standard advertising clause) + public domain | Verbatim notice, non-removable without Basis Technology's permission | Not bundled — **needs a human decision, not just documentation, if it ever is** |

Full per-component detail, exact source URLs, and every license's complete
text are in `packaging/THIRD_PARTY.md`.

---

## Can you distribute Salvage for free, publicly, as-is?

**Yes.** Every bundled component's license permits free public
redistribution of the unmodified binary. What must accompany it, all now
done in `packaging/THIRD_PARTY.md`:

1. **License texts** for every distinct license in use (GPLv2, GPLv3+LGPLv3,
   LGPLv2.1, Apache-2.0 today; CPL-1.0/IPL-1.0 only if the Sleuth Kit gap
   closes).
2. **Copyright/attribution notices** for each component, naming its actual
   author/project — not Assembly Growth.
3. **A written offer of source** for the GPL-covered component (PhotoRec —
   now the only one) — GPLv2 §3(b) specifically requires
   this be an offer *you* stand behind, valid ≥3 years, not just a link to
   someone else's repo (though pointing to the exact upstream tag satisfies
   it in practice as long as your own offer to provide it directly, should
   that link ever die, is also there — which it now is).
4. **"No additional restrictions"** — nothing in Salvage's EULA, ToS, or
   marketing may restrict what a recipient can do with the GPL/LGPL/CPL/IPL
   components specifically, beyond what their own licenses already say.
5. **A Licences/About surface in the app** — specified as a requirement in
   `docs/release-checklist.md` (UI implementation owned separately).

Nothing about free distribution is legally blocked, and with x265 gone
there is no longer a live combined-work question hanging over it.

---

## Can you sell Salvage?

**Yes**, with the same components as above, plus these specifics:

- **GPL components don't prevent selling.** GPLv2 explicitly contemplates
  charging a distribution fee (§1, §3). What it does guarantee is that
  *whoever you sell it to* can redistribute the GPL-covered part (PhotoRec)
  further, including for free — you cannot contract that away. In practice
  this only matters if a customer chooses to extract and re-share that
  specific component; it doesn't stop you charging for Salvage as a product,
  and PhotoRec is already freely available upstream regardless of anything
  you do.
- **LGPL components don't require a commercial license.** PySide6/Qt's LGPL
  option is explicitly designed for exactly this — proprietary or paid apps
  using Qt for free, as long as Qt stays dynamically linked and
  unmodified (both true today). You do not need Qt's commercial license
  tier to sell Salvage.
- **The relinking requirement, concretely:** LGPLv3 §4(d)(1) — the clause
  people call "relinking" — requires that a user of the combined work be
  able to run a modified version of the LGPL library with your app. This is
  satisfied automatically today because PyInstaller keeps Qt's frameworks
  (and every other LGPL library here) as separate `.framework`/`.dylib`
  files in `Contents/Frameworks/`, not merged into the compiled Python
  binary, **and** because `packaging/salvage.spec` doesn't enable Hardened
  Runtime (`codesign_identity=None`, no `--options runtime` anywhere in
  `packaging/`) — so nothing currently stops a user from swapping in a
  modified `.dylib`. **This will need re-checking**: Apple's notarization
  (which `packaging/notarize.sh` sets up, not yet run) *requires* Hardened
  Runtime, and Hardened Runtime's Library Validation would then block an
  unsigned/differently-signed replacement library, undercutting the
  automatic-compliance argument. If/when notarization ships, treat the
  precise, always-available source pointers in `packaging/THIRD_PARTY.md`
  as the operative compliance mechanism (LGPLv2.1 §6(a)/(c), LGPLv3
  §4(d)(0) — "provide the source/object code" instead of "let them
  relink") rather than relying on dynamic linking alone. Worth a specific
  re-check when Hardened Runtime is turned on, not a blocker today.
- **No in-process GPL library any more.** The reason this bullet used to
  read as a warning is that a paid distribution of a *combined work*
  incorporating x265 would have had to extend full GPL rights to every
  paying customer. With x265 removed, the only GPL component is PhotoRec,
  which stays a separate program across a process boundary — so the
  question doesn't arise.

---

## Does invoking GPL binaries via subprocess keep our MIT code separate, or does bundling everything in one installer create a combined work?

This has two genuinely different answers depending on *how* each component
is integrated — they should not be lumped together, and the task of
bundling several components "in one installer" does not by itself create a
combined work under either reading; what matters is how each component
talks to Salvage's own process.

**PhotoRec, and the four libimobiledevice CLI tools — mainstream reading:
separate programs, not a combined work.** `salvage/engine/photorec.py` and
`salvage/engine/ios.py` invoke these as subprocesses (`subprocess.Popen`),
communicating over command-line arguments, a pty/pipe, and a parsed log
file — never shared memory or in-process linking. This is exactly the
pattern the FSF's own GPL FAQ addresses directly:

> "An 'aggregate' consists of a number of separate programs, distributed
> together on the same CD-ROM or other media." Programs communicating via
> "pipes, sockets, command-line arguments" are "normally separate programs."
> (gnu.org/licenses/gpl-faq.html#MereAggregation)

> "A main program that uses simple fork and exec to invoke plug-ins and does
> not establish intimate communication between them results in the plug-ins
> being a separate program." (gnu.org/licenses/gpl-faq.html#GPLPlugins)

Salvage's own code and PhotoRec/libimobiledevice are two separate programs
distributed together — packaging them into one `.app`/one installer doesn't
change that; "aggregation" is specifically about what ships together, and
the FAQ's own example is literally "the same CD-ROM." Salvage's own MIT
source stays MIT under this reading, which is the overwhelming mainstream
practice (this is how essentially every commercial app that shells out to
`ffmpeg`, `git`, or similar GPL tools operates).

**The pillow-heif wheel's x265 — the contested case, now removed rather
than resolved.** Kept here because the reasoning still governs any future
decision to pull in a GPL library that loads in-process, and because it
explains why the bundle looks the way it does. `pillow_heif` was a Python
C-extension module, imported into and running inside the *same process* as
Salvage's own code, which itself dynamically loaded `libheif`, which
dynamically loaded `x265` — all within one address space, not across a
process boundary. The FSF's own position on this is unambiguous and stricter
than the LGPL case:

> "No. Linking a GPL covered work statically or dynamically with other
> modules is making a combined work based on the GPL covered work."
> (gnu.org/licenses/gpl-faq.html#GPLStaticVsDynamic)

Unlike LGPL, plain GPL has no dynamic-linking safe harbor at all — that
mechanism is precisely what distinguishes LGPL from GPL. Under that reading,
Salvage.app as *compiled and distributed* — not Salvage's own source as
written — could have been viewed as a combined work incorporating x265,
which would mean GPLv2's terms applied to that combined work when
distributed. Note that libheif and libde265, which remain, are **LGPL**v3,
where §4(d)(1) grants exactly the dynamic-linking accommodation plain GPL
withholds — the same basis on which Salvage already ships PySide6/Qt. Their
being in-process is therefore not a problem; x265's was.

**Where this is genuinely contested, not just uncomfortable:** the FSF's
FAQ describes their own policy interpretation of their own license, not
settled case law. "Derivative work" is a term from copyright statute, and
whether merely calling into a dynamically-loaded library's API at runtime —
without copying, modifying, or statically incorporating its source or
object code — creates a "derivative work" under the Copyright Act has never
been definitively resolved by a US court specifically on these facts (the
closest analogous cases, e.g. around plugin architectures and APIs, have
gone different ways depending on the specifics). Reasonable, practicing
open-source lawyers disagree on how far dynamic linking alone goes. What's
not contested: it was a meaningfully weaker position than PhotoRec's clean
subprocess separation, and the *conservative, safe* move — regardless of
which legal theory is correct — was to either (a) comply as if the whole
combined distribution had to honor GPLv2 for that component, or (b) remove
the GPL component entirely. **Option (b) is what was done**, which is why
this is no longer a live question and why `packaging/THIRD_PARTY.md` no
longer carries GPLv2 obligations or a written source offer for the HEIC
stack.

**Bottom line:** "bundled in one installer" is not itself the trigger for
either program — what matters is process/library integration, not
packaging. Subprocess tools (PhotoRec, libimobiledevice) are fine under
essentially any reading. The in-process libraries that remain (libheif,
libde265, Qt) are all LGPL, which expressly permits dynamic linking. The one
place where "does this make Salvage's own code GPL" was a live question was
the in-process *GPL* library, x265 — and it is no longer shipped. **Standing
rule for future work:** a GPL (not LGPL) library that loads into Salvage's
own process re-opens this question; one invoked as a subprocess does not.

---

## Practical, lowest-friction compliant path

**Free distribution, today, with the current bundle:** what's already been
done. Ship `packaging/THIRD_PARTY.md` (or surface it via the in-app
Licences screen `docs/release-checklist.md` specifies), keep everything
dynamically linked and unmodified as it is now. No code changes required.

**Free distribution, hardened — DONE (2026-09-18).** This step was to get a
decode-only libheif into the bundle instead of pillow-heif's default wheel.
Done by switching the dependency to `pi-heif`, upstream's own decode-only
wheel. The one contested legal question is gone; everything left in the
bundle is either clean subprocess separation (PhotoRec) or
LGPL/Apache/permissive components with no combined-work issue at all.

**Paid distribution:** same as above, plus make sure the written offer of
source in `packaging/THIRD_PARTY.md` is reachable by an actual paying
customer (in-app Licences screen, not just a GitHub file) — a source offer
nobody can find doesn't satisfy GPLv2 §3(b) in spirit even if it technically
exists in the repo. That offer now covers PhotoRec alone. The x265 lawyer
review this section used to gate a paid launch on is no longer needed; the
component was removed instead.

**The Sleuth Kit, if/when the packaging gap closes:** two options, genuinely
different in effort —
- **Bundle it like PhotoRec** (copy `fls`/`fsstat`/`mmls`/`icat` into
  `Contents/Frameworks/salvage/bin/macos/` in `packaging/build_mac.sh`,
  mirroring the existing `TOOLS` array pattern, and let
  `packaging/relink_macho.py` sweep in `libewf`/`afflib`/`libsqlite3`
  automatically). Legally cheap (CPL-1.0/IPL-1.0 are lenient, libewf is
  ordinary LGPL) except for afflib's non-standard advertising clause, which
  needs a one-time read of `packaging/THIRD_PARTY.md`'s afflib section
  and probably an email to Basis Technology for a waiver.
- **Ship it as a detected, separately-installed dependency** (what
  effectively happens today by accident: `FilesystemEngine.locate_binaries()`
  already falls back to `brew install sleuthkit`'s location). Zero new
  licensing surface, but means Quick/Thorough scan doesn't work out of the
  box for anyone without Homebrew — a real product downside for a
  consumer-facing free/paid app, which is why this document recommends
  fixing the bundling instead. That's a build-script and product decision,
  not made in this pass — see `docs/release-checklist.md`.
