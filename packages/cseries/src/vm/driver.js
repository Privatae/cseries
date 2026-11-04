'use strict';

// The VM layer. A driver puts the C-Series guest on this machine and runs the
// guest's own command, cseries-guest, inside it as root. Everything above this
// layer (the images, the setup code, pairing) talks to the guest only through
// guest() (and the machine wall through guestShell()), so a new OS is a new
// driver and nothing else.
//
// A driver is an object with:
//
//   name, label                  'wsl' / 'WSL'
//   supported                    false for a driver that is announced, not built
//   unsupportedMessage           what to tell the person when it is not
//   preflight(ctx)               OS version and virtualization; throws CliError
//   runtimeReady(ctx)    -> bool the VM software is installed and usable
//   installRuntime(ctx)  -> {halt?: string}   may end the run (a restart)
//   guestExists(ctx)     -> bool
//   guestRunning(ctx)    -> bool
//   createGuest(ctx)             download the guest image and register it
//   startGuest(ctx)              boot it, wait for systemd, keep it running
//   guest(ctx, args, {input, inherit, onOutput}) -> {code, stdout, stderr}
//                                onOutput(chunk): the output as it arrives
//   guestShell(ctx, script) -> {code, stdout, stderr}
//                                /bin/sh as root in the guest, the script on
//                                its standard input: for what an older
//                                guest's cseries-guest cannot do (the machine
//                                wall, machine-firewall.js)
//   stopGuest(ctx)
//   removeGuest(ctx, {deleteData})
//   setAutostart(ctx, on)        start the guest when the person logs in
//   unreachableHint(ctx) -> string   why 127.0.0.1:8800 may not reach the guest
//
// Ports: the guest publishes 8800, 8810 and 8811 on its own 127.0.0.1, and
// the driver makes them, and only them, reachable on the machine's 127.0.0.1.
// The VM's memory and processors are the size the person chose (size.js):
//   sizeLimit(ctx) -> {cpus, memoryGiB, why, note} | null   optional: the
//                                most the VM software lets C-Series have
//   currentSize(ctx) -> {cpus, memoryGiB} | null   optional: the size an
//                                install from before sizes were chosen has
//   resize(ctx, size)            optional: give the VM that size, keeping
//                                its disk; the caller restarts the Host
//   capsInside                   true when the size is a ceiling on
//                                C-Series's units inside the guest (WSL),
//                                not the VM's own size

const { CliError } = require('../errors');
const { PORTS } = require('./common');
const wsl = require('./wsl');
const lima = require('./lima');

function selectDriver(platform = process.platform) {
  if (platform === 'win32') return wsl.driver;
  if (platform === 'darwin') return lima.driver('darwin');
  if (platform === 'linux') return lima.driver('linux');
  return null;
}

function requireSupported(driver, platform = process.platform) {
  if (!driver) {
    throw new CliError(`C-Series does not run on ${platform}.`, 'It runs on Windows, macOS and Linux.');
  }
  if (!driver.supported) throw new CliError(driver.unsupportedMessage, driver.unsupportedNext || '');
  return driver;
}

module.exports = { selectDriver, requireSupported, PORTS };
