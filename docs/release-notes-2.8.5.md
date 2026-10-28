# QiTV 2.8.5

QiTV 2.8.5 keeps channels whose stream restarts every few seconds near live without jumping,
can keep its window above other apps, and starts the phone remote when its own ports are taken.

**Download QiTV from [qitv.app](https://qitv.app), which picks the right build for your
computer, or from [Releases](https://github.com/ozankaraali/QiTV/releases). Please don't use
others' distributions, which may have malware.**

## New

- **Keep on Top.** View → Keep on Top keeps QiTV's window above other apps' windows. QiTV
  remembers it. The mini player is on top anyway, as before.

## Fixed

- **Stay live without the jumps on streams that restart.** Some providers' streams restart
  every 10 to 30 seconds and send seconds again that you have already seen. QiTV drops what
  it can measure of that, but on one provider part of each repeat came later at normal
  speed, so the picture fell further behind live with every restart, and Stay live jumped
  back to live every minute or two. On such streams Stay live now takes those seconds back
  at the stream's own restarts, where the picture changes anyway: no jump back to live, no
  change of speed, and less of the repeated pictures. QiTV also waits a moment longer for
  what a restart sends again: on that provider it came a second late, and all of it played.
- **A channel whose provider allows one stream comes back after a break.** When such a
  channel's stream broke off, the provider often refused the reconnect for a few minutes
  (it still counted the connection that broke), and QiTV gave up at once: "Reconnecting…"
  until you tuned the channel again. QiTV now asks again every 15 seconds for up to 7
  minutes, while what was buffered plays.
- **The phone remote starts when its own ports are taken.** When another program held
  QiTV's ports (5384 to 5393), the phone remote never started and the pairing screen said
  "Starting the phone remote…" for good. It now takes a free port the system gives, the QR
  code tells your phone which, and QiTV remembers it. If it still can't start, the pairing
  screen says so and the log says why.
