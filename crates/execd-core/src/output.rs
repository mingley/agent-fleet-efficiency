//! Output decoding, scrubbing, and bounding helpers.
//!
//! Mirrors upstream `_strip_control_chars` (ANSI strip + `\r\n`→`\n`) and
//! `errors="backslashreplace"` decoding, plus the bounded-output cap from
//! `docs/architecture.md`.

/// Decode bytes like Python `errors="backslashreplace"`: valid UTF-8 passes
/// through, each invalid byte becomes `\xNN` (lowercase hex).
pub fn decode_backslashreplace(bytes: &[u8]) -> String {
    let mut out = String::with_capacity(bytes.len());
    for chunk in bytes.utf8_chunks() {
        out.push_str(chunk.valid());
        for &b in chunk.invalid() {
            out.push_str(&format!("\\x{b:02x}"));
        }
    }
    out
}

/// Length of the trailing incomplete UTF-8 sequence in `b` (0–3 bytes).
/// Used to hold back a split multi-byte char across reads.
pub fn incomplete_tail_len(b: &[u8]) -> usize {
    let max = b.len().min(3);
    for len in 1..=max {
        let s = &b[b.len() - len..];
        let first = s[0];
        if !s[1..].iter().all(|c| c & 0xC0 == 0x80) {
            continue;
        }
        let expected = if first & 0x80 == 0 {
            1
        } else if first & 0xE0 == 0xC0 {
            2
        } else if first & 0xF0 == 0xE0 {
            3
        } else if first & 0xF8 == 0xF0 {
            4
        } else {
            continue;
        };
        if expected > len {
            return len;
        }
    }
    0
}

/// Strip ANSI escape sequences (`\x1B[@-_][0-?]*[ -/]*[@-~]`, same as
/// upstream `_strip_control_chars`) and normalize `\r\n`→`\n`.
pub fn strip_control_chars(s: &str) -> String {
    let b = s.as_bytes();
    let mut out = String::with_capacity(s.len());
    let mut i = 0;
    while i < b.len() {
        if b[i] == 0x1B && i + 1 < b.len() && (0x40..=0x5F).contains(&b[i + 1]) {
            // ANSI escape: ESC + [@-_] + [0-?]* + [ -/]* + [@-~]
            let mut j = i + 2;
            while j < b.len() && (0x30..=0x3F).contains(&b[j]) {
                j += 1;
            }
            while j < b.len() && (0x20..=0x2F).contains(&b[j]) {
                j += 1;
            }
            if j < b.len() && (0x40..=0x7E).contains(&b[j]) {
                j += 1;
                i = j;
                continue;
            }
            // Unterminated: treat ESC as a normal char.
        }
        out.push(b[i] as char);
        i += 1;
    }
    out.replace("\r\n", "\n")
}

/// Remove `EXITCODESTART<seq>:<code>EXITCODEEND` framing artifacts from text
/// (used for interrupt observations, which peek at a buffer that may contain
/// an in-flight run's marker).
pub fn scrub_markers(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(start) = rest.find(crate::EXIT_CODE_PREFIX) {
        let after = &rest[start + crate::EXIT_CODE_PREFIX.len()..];
        if let Some(scrubbed_len) = marker_len(after) {
            out.push_str(&rest[..start]);
            rest = &after[scrubbed_len..];
        } else {
            out.push_str(&rest[..start + crate::EXIT_CODE_PREFIX.len()]);
            rest = after;
        }
    }
    out.push_str(rest);
    out
}

/// Drop text through the last exit framing older than `seq`. A command that
/// outlived its timeout still prints its marker late; without this cut its
/// marker (and the shell's job message) pollutes the next command's output.
pub fn drop_stale_framings(s: &str, seq: u64) -> &str {
    let mut cut: Option<usize> = None;
    let mut rest = s;
    let mut base = 0;
    while let Some(start) = rest.find(crate::EXIT_CODE_PREFIX) {
        let abs_start = base + start;
        let after = &rest[start + crate::EXIT_CODE_PREFIX.len()..];
        if let Some((mark_seq, len)) = marker_seq_len(after) {
            if mark_seq < seq {
                cut = Some(abs_start + crate::EXIT_CODE_PREFIX.len() + len);
            }
            rest = &after[len..];
            base = abs_start + crate::EXIT_CODE_PREFIX.len() + len;
        } else {
            rest = after;
            base = abs_start + crate::EXIT_CODE_PREFIX.len();
        }
    }
    match cut {
        Some(c) => &s[c..],
        None => s,
    }
}

/// If `after` starts with `<digits>:<digits>EXITCODEEND`, return
/// (sequence, length).
fn marker_seq_len(after: &str) -> Option<(u64, usize)> {
    let b = after.as_bytes();
    let mut i = 0;
    while i < b.len() && b[i].is_ascii_digit() {
        i += 1;
    }
    if i == 0 || i >= b.len() || b[i] != b':' {
        return None;
    }
    let seq: u64 = after[..i].parse().ok()?;
    i += 1;
    let code_start = i;
    while i < b.len() && b[i].is_ascii_digit() {
        i += 1;
    }
    if i == code_start || !after[i..].starts_with(crate::EXIT_CODE_SUFFIX) {
        return None;
    }
    Some((seq, i + crate::EXIT_CODE_SUFFIX.len()))
}

/// If `after` starts with `<digits>:<digits>EXITCODEEND`, return its length.
fn marker_len(after: &str) -> Option<usize> {
    marker_seq_len(after).map(|(_, len)| len)
}

/// Approximate Python `repr()` for short message strings: printable text
/// as-is with backslash escapes, preferring single quotes.
pub fn py_repr(s: &str) -> String {
    let mut esc = String::with_capacity(s.len() + 2);
    for c in s.chars() {
        match c {
            '\\' => esc.push_str("\\\\"),
            '\n' => esc.push_str("\\n"),
            '\r' => esc.push_str("\\r"),
            '\t' => esc.push_str("\\t"),
            c if c.is_control() => esc.push_str(&format!("\\x{:02x}", c as u32)),
            c => esc.push(c),
        }
    }
    if s.contains('\'') && !s.contains('"') {
        format!("\"{esc}\"")
    } else {
        format!("'{esc}'")
    }
}

/// Truncate `output` to the last `cap` bytes (char boundary) with a note
/// prefix when over cap. Returns `(text, truncated)`.
pub fn truncate_with_note(output: &str, cap: usize) -> (String, bool) {
    if output.len() <= cap {
        return (output.to_string(), false);
    }
    let mut start = output.len() - cap;
    while start < output.len() && !output.is_char_boundary(start) {
        start += 1;
    }
    let kept = output.len() - start;
    (
        format!(
            "[execd: output truncated (showing last {kept} of {} bytes)]\n{}",
            output.len(),
            &output[start..]
        ),
        true,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn decode_valid_and_invalid() {
        assert_eq!(decode_backslashreplace(b"hi"), "hi");
        assert_eq!(decode_backslashreplace(b"a\xff\xfez"), "a\\xff\\xfez");
        assert_eq!(decode_backslashreplace("héllo".as_bytes()), "héllo");
    }

    #[test]
    fn tail_split_detection() {
        assert_eq!(incomplete_tail_len(b"abc"), 0);
        assert_eq!(incomplete_tail_len("hé".as_bytes()), 0);
        let e_acute = "é".as_bytes();
        assert_eq!(incomplete_tail_len(&e_acute[..1]), 1);
        assert_eq!(incomplete_tail_len(b"\xff"), 0); // invalid, not incomplete
    }

    #[test]
    fn ansi_and_crlf_stripping() {
        assert_eq!(strip_control_chars("\x1b[32mgreen\x1b[0m"), "green");
        assert_eq!(strip_control_chars("a\r\nb"), "a\nb");
        assert_eq!(strip_control_chars("\x1b[@-_"), "-_"); // `\x1b[@` is complete
        assert_eq!(strip_control_chars("a\x1b["), "a\x1b["); // unterminated kept
    }

    #[test]
    fn stale_framing_cut() {
        let s = "Killed: 9 sleep\nEXITCODESTART0:137EXITCODEEND\nrecovered\n";
        assert_eq!(drop_stale_framings(s, 1), "\nrecovered\n");
        assert_eq!(drop_stale_framings(s, 0), s); // nothing older than 0
        assert_eq!(drop_stale_framings("clean output\n", 7), "clean output\n");
        let two = "EXITCODESTART0:1EXITCODEEND\nmid\nEXITCODESTART1:0EXITCODEEND\nnew\n";
        assert_eq!(drop_stale_framings(two, 2), "\nnew\n");
        assert_eq!(
            drop_stale_framings(two, 1),
            "\nmid\nEXITCODESTART1:0EXITCODEEND\nnew\n"
        );
    }

    #[test]
    fn marker_scrubbing() {
        assert_eq!(
            scrub_markers("hi\nEXITCODESTART12:130EXITCODEEND\nbye"),
            "hi\n\nbye"
        );
        assert_eq!(scrub_markers("no markers"), "no markers");
        assert_eq!(scrub_markers("EXITCODESTARTxx"), "EXITCODESTARTxx");
    }

    #[test]
    fn python_repr_shape() {
        assert_eq!(py_repr("sleep 30"), "'sleep 30'");
        assert_eq!(py_repr("it's"), "\"it's\"");
        assert_eq!(py_repr("a\nb"), "'a\\nb'");
    }

    #[test]
    fn truncation_note() {
        let (s, t) = truncate_with_note("abcdef", 100);
        assert!(!t && s == "abcdef");
        let (s, t) = truncate_with_note("abcdef", 3);
        assert!(t && s.ends_with("def") && s.contains("truncated"));
    }
}
