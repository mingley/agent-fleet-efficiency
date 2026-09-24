//! Process-group helpers: tree kills and reaping via `nix`.
//!
//! The PTY child is spawned as a session leader (`setsid` in portable-pty),
//! so its pid == its pgid and `killpg` reaches the whole tree.

use nix::sys::signal::{killpg, Signal};
use nix::unistd::Pid;
use std::os::fd::{BorrowedFd, RawFd};

/// Best-effort `setpgid(pid, pid)`: ensure the shell leads its own process
/// group so `killpg` reliably covers the tree. Portable-pty already calls
/// `setsid` in the child, so failure here is non-fatal (returns the error
/// for logging, caller decides).
pub fn ensure_new_process_group(pid: u32) -> nix::Result<()> {
    let pid = Pid::from_raw(pid as i32);
    nix::unistd::setpgid(pid, pid)
}

/// Foreground process group of the PTY, if queryable.
pub fn foreground_pgid(master_fd: RawFd) -> Option<Pid> {
    if master_fd < 0 {
        return None;
    }
    // SAFETY: borrowed for the duration of the syscall; the master side is
    // owned by the session and outlives the call.
    let fd: BorrowedFd<'_> = unsafe { BorrowedFd::borrow_raw(master_fd) };
    nix::unistd::tcgetpgrp(fd).ok()
}

/// `killpg(pgid, sig)`, tolerating ESRCH (group already gone). Returns true
/// if the signal was (probably) delivered.
pub fn kill_process_group(pgid: Pid, sig: Signal) -> bool {
    match killpg(pgid, sig) {
        Ok(()) => true,
        Err(nix::Error::ESRCH) => false,
        Err(_) => false,
    }
}

/// SIGKILL the foreground process group unless it is the shell itself.
/// Returns true if a kill was attempted.
pub fn kill_foreground(master_fd: Option<RawFd>, shell_pgid: Pid, sig: Signal) -> bool {
    let fg = master_fd.and_then(foreground_pgid);
    match fg {
        Some(pgid) if pgid != shell_pgid && pgid.as_raw() > 0 => kill_process_group(pgid, sig),
        _ => false,
    }
}

/// Reap a child, waiting up to `grace` for it to exit. Returns true if the
/// child was reaped (or was already gone).
pub fn reap_child(
    child: &mut Box<dyn portable_pty::Child + Send + Sync>,
    grace: std::time::Duration,
) -> bool {
    let deadline = std::time::Instant::now() + grace;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return true,
            Ok(None) => {
                if std::time::Instant::now() >= deadline {
                    return false;
                }
                std::thread::sleep(std::time::Duration::from_millis(10));
            }
            Err(_) => return true,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn killpg_esrch_tolerated() {
        // Assumes no process group 2^30-1 exists.
        assert!(!kill_process_group(Pid::from_raw(1 << 30), Signal::SIGKILL));
    }

    #[test]
    fn foreground_pgid_bad_fd_is_none() {
        assert_eq!(foreground_pgid(-1), None);
    }
}
