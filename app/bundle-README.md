# moto-tracker — installing it on the second Mac

This folder installs the moto-tracker app on a Mac. It is the same app that runs on
the first Mac: the live map, the incident alarm, and the menu-bar icon.

It is one app and nothing else. It talks to the relay directly, the way the phones
do — there is no background service to start first and nothing to wait for.

Everything it needs is in here except one thing — a **pairing code**, which comes
from the first Mac and takes ten seconds to make. There is nothing to configure and
no account to create. (A Mac that was already paired keeps its pairing when updated.)

---

## What this Mac will do

Once installed, this Mac:

* shows both riders' panes — the last trip's date, times, duration, top and average
  speed, and the hardest forces recorded;
* switches to a live map the moment either rider starts a trip, following them with
  traffic and weather;
* **sounds an alarm** if a crash is detected, whether or not the window is open,
  brings itself to the front and wakes the display;
* lets whoever is here silence that alarm and close the incident.

It is a **viewer**. The first Mac keeps the history — every trip, every log file —
so this one has no *Open logs* button and no *Devices* button. Nothing else differs.

---

## What is in this folder

| | |
|---|---|
| `Install moto-tracker.command` | the installer — run it from Terminal, see step 2 |
| `app/` | the app's source, which the installer compiles here |
| `pki/ca.crt` | the certificate that proves the relay is ours (public — not a password) |
| `monitor-secrets/` | the map key, if it was included (see *Privacy*, below) |

---

## Before you start

* **macOS 13 or newer.**
* **Apple's command line tools.** Most Macs already have them. If not, the installer
  says so and opens the dialog for you — click *Install*, wait for it to finish
  (a few minutes), then run the installer again. They provide the Swift compiler the
  installer uses to build the app on this Mac.
* You will be asked for **nothing else**: no password, no Apple ID, no account.

---

## Installing

1. Unzip the folder onto this Mac — the Desktop is fine.

2. **Open Terminal** (⌘-Space, type `Terminal`, press return) and run the installer
   by hand. This is the reliable way, and it takes one line: type `bash ` (with the
   space), then **drag the `Install moto-tracker.command` file from the Finder onto
   the Terminal window** — that fills in the path for you — then press return. It
   looks like this:

   ```
   bash ~/Desktop/moto-tracker-for-her-mac/Install\ moto-tracker.command
   ```

   Do it this way even if double-clicking seems to work. macOS refuses to launch
   anything carried over from another Mac — you get *"damaged and should be moved to
   the Trash"* — and that block applies to double-clicking, not to running the file
   through Terminal. The installer's first act is to clear that mark from the folder,
   and it then **builds the app here on this Mac**, so nothing that arrived from
   elsewhere ever has to launch.

3. Watch the output. It prints five numbered steps and takes about a minute. If it
   asks for Apple's command line tools, click **Install** in the dialog, wait for it
   to finish, and run the same line again.

4. When it finishes, the **moto-tracker** window opens by itself.

The installer puts the app in `/Applications` and sets it to open whenever the Mac
starts — and to reopen by itself at once if it ever crashes. You never have to launch
anything by hand afterwards. If this Mac still has the old background service from an
earlier version, the installer retires it.

---

## Updating it later

Exactly the same way. Unzip the new folder and run the installer again:

    bash ~/Desktop/moto-tracker-for-her-mac/Install\ moto-tracker.command

It replaces the app and **leaves this Mac's pairing alone** — the key lives in this
Mac's own user folder, not in the bundle, so it is never touched. Nobody has to pair
anything a second time. The window reopens by itself when it is done.

---

## Pairing it (the only step that needs the other Mac)

The window will say **Pair this Mac**.

1. On the **first Mac**, in the moto-tracker window: click the **⚙ gear** in the
   top-right corner → **Devices**.
2. Set **Role** to **Monitor (this Mac)**, type a name (for example
   `Dana's Mac`), and click **Create pairing code**.
3. An eight-character code appears, like `K7PQ-3RTM`. Read it to whoever is at the
   second Mac.
4. Type it into the **Code** box on the second Mac and click **Pair this Mac**.

The code works **once** and expires after **ten minutes**. If it expires, just make
another one. When pairing succeeds the window says so and the live screen appears.

That is the entire setup. The Mac now has its own key, kept in its own user folder.
No key was ever copied between the two machines.

---

## Using it

**The window.** Closing it does *not* quit the app — it only hides it, and the alarm
still works. Click the **moto-tracker icon in the menu bar** (top-right of the
screen, near the clock) to bring the window back.

**Quitting** asks first, because while moto-tracker is quit this Mac neither sounds
the alarm nor flashes the icon. It opens again at the next login, or from
Applications.

**The menu-bar icon flashes** while an incident is open: red for a crash, yellow once
the rider has said they are OK.

**When a crash is detected**, this Mac will:

1. sound an alarm through the speakers — it raises the volume if it is turned down;
2. wake the screen and bring the window to the front;
3. show a red box with the rider's **last known position as a Google Maps link**
   (and a copy button), how fast they were going before and after, how long they have
   been stationary, and the forces recorded.

In that box:

* **Silence alarm** stops the noise everywhere — the other Mac and the other phone
  too — but leaves the incident open. Use it as soon as you know what is happening.
* **Close incident** is your half of closing it. The incident stays open until the
  rider closes their half as well, on their phone. That is deliberate: it makes the
  two of you actually speak to each other before the alert is dismissed.

**The ⚙ gear menu** has **Test alarm** (it keeps sounding until you press it again)
and **Test incident**, which rehearses the whole thing on this Mac without telling
the relay, the riders or anyone else. Use them once, now, so the sound is familiar
and you know the volume carries.

---

## If something is wrong

**"This file is damaged and should be moved to the Trash", or "cannot be
opened"** — that is macOS refusing a file that came from another Mac, not a broken
download. Run the installer from Terminal as in step 2 above; that route is not
blocked. (If you would rather clear the mark first, run
`xattr -dr com.apple.quarantine ~/Desktop/moto-tracker-for-her-mac` and then
double-click the installer.)

**The window looks frozen** — choose **Reload** from the moto-tracker menu (⌘R). If
the app as a whole ever stops responding, it notices within two minutes, closes
itself and reopens.

**"Not paired yet"** — see *Pairing*, above.

**"Key rejected"** — this Mac's key was revoked, or the relay was rebuilt. Make a
new pairing code on the first Mac and pair again.

**"Server unreachable" (red light)** — this Mac cannot reach the relay. Check the
internet connection here first; if other sites work, ask the first Mac whether it is
green too.

**The map is black with only a moving dot** — the map key was not included in this
bundle. Everything else works; ask for a bundle with the key, or drop a `tomtom.key`
file into `~/Library/Application Support/moto-tracker/monitor-secrets/`.

**The alarm is too quiet** — it raises the system volume to 70% when it fires, but
it cannot help external speakers being switched off. Use *Test alarm* in the gear
menu to check the level from the next room.

---

## Privacy — what leaves this Mac

Nothing on this Mac listens for connections at all. The app talks outward to three
places:

* **the relay**, over an encrypted connection it trusts only because of the
  certificate in `pki/`, using this Mac's own key;
* **the weather service**, with the rider's position **rounded to about a
  kilometre**, so an exact location is never sent;
* **the map service**, for map tiles — the app adds the map key itself, so the key
  never reaches the page.

If this bundle includes `monitor-secrets/tomtom.key`, treat the folder itself as
private and delete it after installing.

---

## Stopping or removing it

To stop it opening at login: open Terminal and run

    bash ~/Library/Application\ Support/moto-tracker/install.sh --uninstall

That quits the app and removes the login item, and leaves the app and the data alone.
To remove the app as well, drag `/Applications/moto-tracker.app` to the Bin.
