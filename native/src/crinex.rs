//! Hatanaka-compressed RINEX (Compact RINEX 1.0, 3.0 and 3.1) decoded inside the core: `crx2rnx`
//! writes the RINEX observation file that the CRX2RNX of RNXCMP 4.2.0 writes, byte for byte (the
//! files of tests/data/crinex). Written from the description of the format alone: Hatanaka (2008),
//! "A Compression Format and Tools for GNSS Observation Data", Bulletin of the Geographical Survey
//! Institute 55, 21-30, appendices 1-3, and the CHANGES of RNXCMP 4.2.0 for Compact RINEX 3.1 (the
//! picoseconds of the epoch, after the receiver clock offset).
//!
//! Where it differs from CRX2RNX 4.2.0: a negative receiver clock offset that is a whole number of
//! tenths of a millisecond (RINEX 3) or of a second (RINEX 2) is written as the file holds it (CRX2RNX
//! writes it one tenth nearer zero, -0.001 s as -.000900000000); a file whose last line has no line end
//! was cut short and is refused (CRX2RNX decodes what it finds).

use std::collections::{HashMap, VecDeque};
use std::fs::{self, File};
use std::io::{self, BufRead, BufReader, BufWriter, Read, Write};
use std::path::{Path, PathBuf};

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;

/// The highest order of the differences of a numeric series (the format allows up to 9).
const MAX_ORDER: usize = 9;

/// The longest line read: a line of Compact RINEX is some hundreds of bytes at most (an epoch line
/// lists 3 bytes a satellite, the line of a satellite some tens an observation type). A longer one is
/// no Compact RINEX, and a file of gigabytes without a line end is refused here, not read whole.
const MAX_LINE: usize = 1 << 20;

/// What stops the decoding: the compressed file is not what the format allows (and the line where
/// that shows), or a file cannot be read or written.
enum Failure {
    Format { line: usize, what: String },
    Io(io::Error),
}

impl From<io::Error> for Failure {
    fn from(error: io::Error) -> Self {
        Failure::Io(error)
    }
}

type Result<T> = std::result::Result<T, Failure>;

fn refuse<T>(line: usize, what: impl Into<String>) -> Result<T> {
    Err(Failure::Format { line, what: what.into() })
}

/// `text` for a message, what is not printable escaped.
fn shown(text: &[u8]) -> String {
    text.escape_ascii().to_string()
}

fn trim_end(text: &[u8]) -> &[u8] {
    let end = text.iter().rposition(|&c| c != b' ').map_or(0, |i| i + 1);
    &text[..end]
}

/// One line of the compressed file without its end (LF or CR LF), and whether it had one: the last
/// line of a file that was cut short has none.
struct Line {
    text: Vec<u8>,
    ended: bool,
}

/// The lines of the compressed file, numbered from 1; the lines read ahead wait in `ahead`.
struct Lines<R> {
    input: R,
    number: usize,
    ahead: VecDeque<Line>,
}

impl<R: BufRead> Lines<R> {
    fn read(&mut self) -> Result<Option<Line>> {
        let mut text = Vec::new();
        let limit = (MAX_LINE + 2) as u64; // the line and its end, CR LF
        if (&mut self.input).take(limit).read_until(b'\n', &mut text)? == 0 {
            return Ok(None);
        }
        let ended = text.last() == Some(&b'\n');
        if !ended && text.len() as u64 >= limit {
            let number = self.number + self.ahead.len() + 1;
            return refuse(number, format!("a line longer than {MAX_LINE} bytes: not Compact RINEX"));
        }
        if ended {
            text.pop();
            if text.last() == Some(&b'\r') {
                text.pop();
            }
        }
        Ok(Some(Line { text, ended }))
    }

    /// The next line, None at the end of the file; `number` is then its number.
    fn next(&mut self) -> Result<Option<Line>> {
        let line = match self.ahead.pop_front() {
            Some(line) => Some(line),
            None => self.read()?,
        };
        if line.is_some() {
            self.number += 1;
        }
        Ok(line)
    }

    /// The next line, which has to be there and whole: `what` it holds goes in the message.
    fn need(&mut self, what: &str) -> Result<Vec<u8>> {
        match self.next()? {
            Some(Line { text, ended: true }) => Ok(text),
            Some(_) => refuse(self.number, "the file ends in the middle of this line (cut short?)"),
            None => refuse(self.number + 1, format!("the file ends before {what} (cut short?)")),
        }
    }

    /// Nothing but empty lines up to the end of the file (they are no epochs: the data are over).
    fn only_empty_lines_left(&mut self) -> Result<bool> {
        if self.ahead.iter().any(|line| !line.text.is_empty()) {
            return Ok(false);
        }
        while let Some(line) = self.read()? {
            let empty = line.text.is_empty();
            self.ahead.push_back(line);
            if !empty {
                return Ok(false);
            }
        }
        Ok(true)
    }
}

/// A numeric series (the receiver clock offset, or one observation of a satellite) as the decoder
/// holds it: the last value and its differences up to the order of the series (appendix 1).
#[derive(Clone)]
struct Series {
    order: usize,
    count: usize,
    d: [i64; MAX_ORDER + 1],
}

impl Series {
    fn start(order: usize, value: i64) -> Series {
        let mut d = [0; MAX_ORDER + 1];
        d[0] = value;
        Series { order, count: 1, d }
    }

    /// The next value, from its difference of order min(values so far, order of the series); None
    /// when a sum overflows (a damaged file).
    fn add(&mut self, difference: i64) -> Option<i64> {
        let m = self.count.min(self.order);
        self.d[m] = difference;
        for j in (0..m).rev() {
            self.d[j] = self.d[j].checked_add(self.d[j + 1])?;
        }
        if self.count <= self.order {
            self.count += 1;
        }
        Some(self.d[0])
    }

    fn value(&self) -> i64 {
        self.d[0]
    }
}

/// A numeric field of the compressed file: blank, the start of a series ("order&value"), or the next
/// difference of a series (an integer: no plus sign, no space).
enum Number {
    Blank,
    Start(usize, i64),
    Difference(i64),
}

fn integer(text: &[u8]) -> Option<i64> {
    let (negative, digits) = match text.split_first() {
        Some((b'-', rest)) => (true, rest),
        _ => (false, text),
    };
    if digits.is_empty() || !digits.iter().all(u8::is_ascii_digit) {
        return None;
    }
    let mut value: i64 = 0;
    for &c in digits {
        value = value.checked_mul(10)?.checked_add(i64::from(c - b'0'))?;
    }
    Some(if negative { -value } else { value })
}

fn number(text: &[u8]) -> Option<Number> {
    if text.is_empty() {
        return Some(Number::Blank);
    }
    if text.len() > 2 && text[1] == b'&' {
        let order = usize::from(text[0].checked_sub(b'0')?);
        if order > MAX_ORDER {
            return None;
        }
        return integer(&text[2..]).map(|value| Number::Start(order, value));
    }
    integer(text).map(Number::Difference)
}

/// `text` changed as the differenced text `diff` says (section 2.2): a space keeps the character,
/// "&" makes it a space, any other character takes its place; `text` grows with spaces if needed.
fn apply(text: &mut Vec<u8>, diff: &[u8]) {
    if text.len() < diff.len() {
        text.resize(diff.len(), b' ');
    }
    for (t, &d) in text.iter_mut().zip(diff) {
        match d {
            b' ' => {}
            b'&' => *t = b' ',
            c => *t = c,
        }
    }
}

/// `value`, in units of 10^-decimals, as CRX2RNX writes it, right-aligned in `width` columns: the
/// integer part left out when it is zero (".123", "-.123"). False when it does not fit.
fn fixed(out: &mut Vec<u8>, value: i64, decimals: usize, width: usize) -> bool {
    let scale = 10u64.pow(decimals as u32);
    let magnitude = value.unsigned_abs();
    let sign = if value < 0 { "-" } else { "" };
    let text = if magnitude < scale {
        format!("{sign}.{:0decimals$}", magnitude)
    } else {
        format!("{sign}{}.{:0decimals$}", magnitude / scale, magnitude % scale)
    };
    if text.len() > width {
        return false;
    }
    out.resize(out.len() + width - text.len(), b' ');
    out.extend_from_slice(text.as_bytes());
    true
}

fn label(line: &[u8]) -> &[u8] {
    line.get(60..).unwrap_or(&[])
}

/// An integer field of a RINEX line (columns `range`), blank as 0; None when it is not a number.
fn count_in(text: &[u8], range: std::ops::Range<usize>) -> Option<usize> {
    let digits = text.get(range.start..range.end.min(text.len())).unwrap_or(&[]).trim_ascii();
    if digits.is_empty() {
        return Some(0);
    }
    integer(digits).and_then(|v| usize::try_from(v).ok())
}

/// The Compact RINEX version, which says where its epoch lines start over and what follows the clock.
#[derive(Clone, Copy, PartialEq)]
enum Version {
    /// RINEX 2: "&" in the first column of an epoch line given in full.
    One,
    /// RINEX 3 and 4: ">" in the first column of an epoch line given in full, "&" an optional line.
    Three,
    /// RINEX 4.02 and later: as 3.0, and the picoseconds of the epoch after the clock offset.
    ThreeOne,
}

/// What a satellite carries from one epoch to the next: its observation series, and its indicators
/// (LLI and signal strength of every observation type) as text.
#[derive(Default)]
struct Satellite {
    observations: Vec<Option<Series>>,
    flags: Vec<u8>,
}

/// How an epoch line of the compressed file is to be read.
enum EpochLine {
    /// Given in full: every series starts over.
    Full(Vec<u8>),
    /// An event (flag 2 to 6), copied with the records that follow it.
    Event(Vec<u8>),
    /// Differenced from the epoch line before it.
    Difference(Vec<u8>),
}

struct Decoder<R, W> {
    lines: Lines<R>,
    out: W,
    version: Version,
    /// RINEX 2: the observation types of every satellite.
    types: usize,
    /// RINEX 3 and 4: the observation types of each system.
    system_types: HashMap<u8, usize>,
    /// The epoch line in full, with every satellite of the epoch.
    epoch: Vec<u8>,
    clock: Option<Series>,
    /// Compact RINEX 3.1: the picoseconds of the epoch, as text (CRX2RNX keeps them once given).
    picoseconds: Vec<u8>,
    satellites: HashMap<[u8; 3], Satellite>,
    /// The start of the data or an event: the next epoch line has to be given in full.
    full_epoch_due: bool,
}

impl<R: BufRead, W: Write> Decoder<R, W> {
    fn new(input: R, out: W) -> Self {
        Decoder {
            lines: Lines { input, number: 0, ahead: VecDeque::new() },
            out,
            version: Version::One,
            types: 0,
            system_types: HashMap::new(),
            epoch: Vec::new(),
            clock: None,
            picoseconds: Vec::new(),
            satellites: HashMap::new(),
            full_epoch_due: true,
        }
    }

    fn write_line(&mut self, text: &[u8]) -> Result<()> {
        self.out.write_all(trim_end(text))?;
        self.out.write_all(b"\n")?;
        Ok(())
    }

    fn rinex2(&self) -> bool {
        self.version == Version::One
    }

    /// The two lines of Compact RINEX, then the RINEX header as it is, up to END OF HEADER.
    fn header(&mut self) -> Result<()> {
        let first = match self.lines.next()? {
            Some(line) => line,
            None => return refuse(1, "the file is empty, not Compact RINEX (Hatanaka)"),
        };
        if !label(&first.text).starts_with(b"CRINEX VERS") {
            return refuse(1, "not Compact RINEX (Hatanaka): its first line is not CRINEX VERS / TYPE");
        }
        let version = shown(first.text.get(..20).unwrap_or(&first.text).trim_ascii());
        self.version = match version.as_str() {
            "1.0" => Version::One,
            "3.0" => Version::Three,
            "3.1" => Version::ThreeOne,
            _ => return refuse(1, format!("Compact RINEX version {version}, not one of those known (1.0, 3.0, 3.1)")),
        };
        if !first.ended {
            return refuse(1, "the file ends in the middle of this line (cut short?)");
        }
        let second = self.lines.need("CRINEX PROG / DATE")?;
        if !label(&second).starts_with(b"CRINEX PROG") {
            return refuse(2, "no CRINEX PROG / DATE after CRINEX VERS / TYPE");
        }
        let mut rinex_version = false;
        loop {
            let line = self.lines.need("END OF HEADER")?;
            let number = self.lines.number;
            if label(&line).starts_with(b"RINEX VERSION / TYPE") {
                self.rinex_version(&line, number)?;
                rinex_version = true;
            }
            self.header_record(&line, number)?;
            self.write_line_as_it_is(&line)?;
            if label(&line).starts_with(b"END OF HEADER") {
                let number = self.lines.number;
                if !rinex_version {
                    return refuse(number, "the RINEX header has no RINEX VERSION / TYPE");
                }
                if (self.rinex2() && self.types == 0) || (!self.rinex2() && self.system_types.is_empty()) {
                    return refuse(number, "the RINEX header gives no observation types");
                }
                return Ok(());
            }
        }
    }

    fn write_line_as_it_is(&mut self, text: &[u8]) -> Result<()> {
        self.out.write_all(text)?;
        self.out.write_all(b"\n")?;
        Ok(())
    }

    /// RINEX VERSION / TYPE: observations, of the RINEX the Compact RINEX version holds (1.0: RINEX 2;
    /// 3.0 and 3.1: RINEX 3 and 4).
    fn rinex_version(&self, line: &[u8], number: usize) -> Result<()> {
        let text = shown(line.get(..9).unwrap_or(line).trim_ascii());
        let major = text.split('.').next().and_then(|m| m.parse::<u32>().ok());
        match major {
            Some(2) if self.rinex2() => {}
            Some(3..) if !self.rinex2() => {}
            _ => {
                let held = if self.rinex2() { "RINEX 2" } else { "RINEX 3 or 4" };
                return refuse(number, format!("RINEX version {text}, where this Compact RINEX holds {held}"));
            }
        }
        match line.get(20) {
            Some(b'O') => Ok(()),
            _ => refuse(number, "not a RINEX observation file: Compact RINEX holds observations only"),
        }
    }

    /// A header record, of the header or of an event: the number of observation types of every
    /// satellite (RINEX 2) or of a system (RINEX 3 and 4); a continuation line leaves it as it is.
    fn header_record(&mut self, line: &[u8], number: usize) -> Result<()> {
        if self.rinex2() && label(line).starts_with(b"# / TYPES OF OBSERV") {
            match count_in(line, 0..6) {
                Some(0) => {}
                Some(n) => self.types = n,
                None => return refuse(number, "the number of observation types is not a number"),
            }
        } else if !self.rinex2() && label(line).starts_with(b"SYS / # / OBS TYPES") {
            let system = line.first().copied().unwrap_or(b' ');
            if system != b' ' {
                match count_in(line, 3..6) {
                    Some(n) => {
                        self.system_types.insert(system, n);
                    }
                    None => return refuse(number, "the number of observation types is not a number"),
                }
            }
        }
        Ok(())
    }

    /// The data records, up to the end of the file.
    fn data(&mut self) -> Result<()> {
        while let Some(line) = self.lines.next()? {
            let number = self.lines.number;
            if !line.ended {
                return refuse(number, "the file ends in the middle of this line (cut short?)");
            }
            if line.text.is_empty() && self.lines.only_empty_lines_left()? {
                return Ok(());
            }
            match self.epoch_line(line.text, number)? {
                None => {} // an optional record (appendix 2, table A-3)
                Some(EpochLine::Event(text)) => self.event(text, number)?,
                Some(EpochLine::Full(text)) => {
                    self.epoch.clear();
                    apply(&mut self.epoch, &text);
                    self.clock = None;
                    self.satellites.clear();
                    self.full_epoch_due = false;
                    self.observations(number)?;
                }
                Some(EpochLine::Difference(text)) => {
                    if self.full_epoch_due {
                        return refuse(number, "an epoch line that differs from one before it, where a whole one \
                                               is due (the start of the data, or after an event)");
                    }
                    apply(&mut self.epoch, &text);
                    self.observations(number)?;
                }
            }
        }
        Ok(())
    }

    /// What the line where an epoch starts is: given in full (">" in the first column in Compact RINEX
    /// 3, "&" in 1.0, the RINEX epoch line then has a space there) or differenced (a space), an event
    /// (an epoch flag from 2 to 6, given in full), or an optional line to skip ("&" in Compact RINEX 3).
    fn epoch_line(&self, mut text: Vec<u8>, number: usize) -> Result<Option<EpochLine>> {
        let (first, flag_at) = (text.first().copied(), if self.rinex2() { 28 } else { 31 });
        let full = match (self.version, first) {
            (Version::One, Some(b'&')) => {
                text[0] = b' ';
                true
            }
            (Version::Three | Version::ThreeOne, Some(b'>')) => true,
            (Version::Three | Version::ThreeOne, Some(b'&')) => return Ok(None),
            (_, None | Some(b' ')) => {
                // an event line of RINEX 2 written as it is, without "&" (flags 2 to 6 are never differenced)
                if self.rinex2() && matches!(text.get(flag_at), Some(b'2'..=b'6')) {
                    return Ok(Some(EpochLine::Event(text)));
                }
                false
            }
            _ => return refuse(number, "not an epoch line"),
        };
        if !full {
            return Ok(Some(EpochLine::Difference(text)));
        }
        match text.get(flag_at) {
            Some(b'0' | b'1') => Ok(Some(EpochLine::Full(text))),
            Some(b'2'..=b'6') => Ok(Some(EpochLine::Event(text))),
            _ => refuse(number, "an epoch line with no epoch flag from 0 to 6"),
        }
    }

    /// An event: its line and the special records that follow, as they are (appendix 2, table A-2);
    /// header records among them may change the observation types. The next epoch starts over.
    fn event(&mut self, text: Vec<u8>, number: usize) -> Result<()> {
        let (flag_at, records_at) = if self.rinex2() { (28, 29..32) } else { (31, 32..35) };
        let records = match count_in(&text, records_at) {
            Some(n) => n,
            None => return refuse(number, "the number of records of the event is not a number"),
        };
        let header = matches!(text.get(flag_at), Some(b'2'..=b'5')); // 6: cycle slip records
        self.write_line_as_it_is(&text)?;
        for _ in 0..records {
            let record = self.lines.need("the records of the event")?;
            if header {
                self.header_record(&record, self.lines.number)?;
            }
            self.write_line_as_it_is(&record)?;
        }
        self.clock = None;
        self.satellites.clear();
        self.full_epoch_due = true;
        Ok(())
    }

    /// An epoch with observations (flag 0 or 1): its epoch line (the clock offset line comes next),
    /// then a line for every satellite of its list.
    fn observations(&mut self, number: usize) -> Result<()> {
        let (flag_at, count_at, list_at) = if self.rinex2() { (28, 29..32, 32) } else { (31, 32..35, 41) };
        if !self.rinex2() && self.epoch.first() != Some(&b'>') {
            return refuse(number, "not an epoch line: no > in its first column");
        }
        match self.epoch.get(flag_at) {
            Some(b'0' | b'1') => {}
            _ => return refuse(number, "an epoch line with observations and no epoch flag 0 or 1"),
        }
        // the number of satellites always ends in a digit, which no trimming takes away: a line that stops before
        // it is cut short, never an epoch without satellites
        if self.epoch.len() < count_at.end {
            return refuse(number, "the epoch line stops before the number of its satellites (cut short?)");
        }
        let count = match count_in(&self.epoch, count_at) {
            Some(n) => n,
            None => return refuse(number, "the number of satellites of the epoch is not a number"),
        };
        // the spaces at the end of a line are gone: with no satellite, the blank columns 36-41 of RINEX 3 too
        if self.epoch.len() < list_at {
            self.epoch.resize(list_at, b' ');
        }
        let list = match self.epoch.get(list_at..list_at + 3 * count) {
            Some(list) => list.to_vec(),
            None => return refuse(number, format!("the epoch line lists fewer satellites than its {count}")),
        };
        let satellites: Vec<[u8; 3]> = list.chunks(3).map(|s| [s[0], s[1], s[2]]).collect();
        self.clock_line()?;
        self.write_epoch(&list)?;
        let mut before = std::mem::take(&mut self.satellites);
        for satellite in satellites {
            if self.satellites.contains_key(&satellite) {
                return refuse(number, format!("satellite {} twice in the epoch", shown(&satellite)));
            }
            let types = if self.rinex2() {
                self.types
            } else {
                match self.system_types.get(&satellite[0]) {
                    Some(&n) => n,
                    None => {
                        return refuse(number, format!("satellite {}: no observation types for its system in \
                                                       the header", shown(&satellite)))
                    }
                }
            };
            let state = before.remove(&satellite).unwrap_or_default(); // not in the epoch before: a new one
            let state = self.satellite(&satellite, state, types)?;
            self.satellites.insert(satellite, state);
        }
        Ok(())
    }

    /// The receiver clock offset (blank, started over, or differenced) and, in Compact RINEX 3.1, the
    /// picoseconds of the epoch after a space, as text.
    fn clock_line(&mut self) -> Result<()> {
        let line = self.lines.need("the clock offset line of the epoch")?;
        let number = self.lines.number;
        let (clock, picoseconds) = match (self.version, line.iter().position(|&c| c == b' ')) {
            (Version::ThreeOne, Some(space)) => (&line[..space], &line[space + 1..]),
            _ => (&line[..], &[][..]),
        };
        match number_field(clock, number)? {
            Number::Blank => self.clock = None,
            Number::Start(order, value) => self.clock = Some(Series::start(order, value)),
            Number::Difference(difference) => match self.clock.as_mut() {
                Some(series) => {
                    if series.add(difference).is_none() {
                        return refuse(number, "a clock offset out of range (the file is damaged)");
                    }
                }
                None => {
                    return refuse(number, "a clock offset difference with no value before it to add it to \
                                           (the start of its series is missing)")
                }
            },
        }
        apply(&mut self.picoseconds, picoseconds);
        Ok(())
    }

    /// The RINEX epoch line. RINEX 3 and 4: the first 41 columns, the clock offset (F15.12) and the
    /// picoseconds (1X,I5). RINEX 2: the first 32 columns and 12 satellites, the clock offset (F12.9) in
    /// columns 69-80, then 12 satellites a line after 32 spaces.
    fn write_epoch(&mut self, list: &[u8]) -> Result<()> {
        let number = self.lines.number; // the clock offset line
        let mut line: Vec<u8> = self.epoch[..self.epoch.len().min(if self.rinex2() { 32 } else { 41 })].to_vec();
        if self.rinex2() {
            line.resize(32, b' ');
            line.extend_from_slice(&list[..list.len().min(36)]);
            if let Some(clock) = &self.clock {
                line.resize(68, b' ');
                if !fixed(&mut line, clock.value(), 9, 12) {
                    return refuse(number, "a clock offset too large for RINEX 2 (F12.9)");
                }
            }
            self.write_line(&line)?;
            for more in list.get(36..).unwrap_or(&[]).chunks(36) {
                let mut next = vec![b' '; 32];
                next.extend_from_slice(more);
                self.write_line(&next)?;
            }
            return Ok(());
        }
        line.resize(41, b' ');
        match &self.clock {
            Some(clock) => {
                if !fixed(&mut line, clock.value(), 12, 15) {
                    return refuse(number, "a clock offset too large for RINEX (F15.12)");
                }
            }
            None => line.resize(56, b' '),
        }
        if self.picoseconds.iter().any(|&c| c != b' ') {
            line.push(b' ');
            line.extend_from_slice(&self.picoseconds);
        }
        self.write_line(&line)
    }

    /// The line of a satellite: `types` observations (blank, started over or differenced), separated by
    /// one space, then its indicators differenced as text (appendix 2, note 3). Compact RINEX 1.0
    /// starts the indicators of an observation over with the observation; 3.0 has them on their own,
    /// also without an observation.
    fn satellite(&mut self, id: &[u8; 3], mut state: Satellite, types: usize) -> Result<Satellite> {
        let line = self.lines.need(&format!("the observations of satellite {}", shown(id)))?;
        let number = self.lines.number;
        state.observations.resize(types, None);
        state.flags.resize(2 * types, b' ');
        let mut rest = &line[..];
        for j in 0..types {
            let field = match rest.iter().position(|&c| c == b' ') {
                Some(space) => {
                    let field = &rest[..space];
                    rest = &rest[space + 1..];
                    field
                }
                None => std::mem::take(&mut rest),
            };
            match number_field(field, number)? {
                Number::Blank => state.observations[j] = None,
                Number::Start(order, value) => {
                    state.observations[j] = Some(Series::start(order, value));
                    if self.rinex2() {
                        state.flags[2 * j..2 * j + 2].copy_from_slice(b"  ");
                    }
                }
                Number::Difference(difference) => match state.observations[j].as_mut() {
                    Some(series) => {
                        if series.add(difference).is_none() {
                            return refuse(number, "an observation out of range (the file is damaged)");
                        }
                    }
                    None => {
                        return refuse(number, format!("satellite {}: a difference with no value before it to \
                                                       add it to (the start of its series is missing)", shown(id)))
                    }
                },
            }
        }
        if rest.len() > 2 * types {
            return refuse(number, format!("satellite {}: more indicators (LLI, signal strength) than its {} \
                                           observation types", shown(id), types));
        }
        apply(&mut state.flags, rest);
        // RINEX 3 and 4: the satellite and its observations on a line; RINEX 2: five a line
        let mut fields: Vec<u8> = Vec::with_capacity(16 * types);
        for (j, observation) in state.observations.iter().enumerate() {
            match observation {
                Some(series) => {
                    if !fixed(&mut fields, series.value(), 3, 14) {
                        return refuse(number, format!("satellite {}: an observation too large for RINEX (F14.3)",
                                                      shown(id)));
                    }
                    fields.extend_from_slice(&state.flags[2 * j..2 * j + 2]);
                }
                None if self.rinex2() => fields.extend_from_slice(&[b' '; 16]),
                None => {
                    fields.extend_from_slice(&[b' '; 14]);
                    fields.extend_from_slice(&state.flags[2 * j..2 * j + 2]);
                }
            }
        }
        if self.rinex2() {
            for five in fields.chunks(80) {
                self.write_line(five)?;
            }
        } else {
            let mut out = id.to_vec();
            out.extend_from_slice(&fields);
            self.write_line(&out)?;
        }
        Ok(state)
    }
}

fn number_field(text: &[u8], line: usize) -> Result<Number> {
    match number(text) {
        Some(n) => Ok(n),
        None => refuse(line, format!("\"{}\" is not a number of Compact RINEX", shown(text))),
    }
}

/// `dst` with ".part" after its name.
fn part_of(dst: &Path) -> PathBuf {
    let mut name = dst.as_os_str().to_owned();
    name.push(".part");
    PathBuf::from(name)
}

/// `error` with the path of the file it is about in its message, of the same kind.
fn about(path: &Path, error: io::Error) -> io::Error {
    io::Error::new(error.kind(), format!("{}: {error}", path.display()))
}

/// A file whose errors name it (`about`): what is read from the compressed file, written to the
/// decoded one.
struct Named<F> {
    file: F,
    path: PathBuf,
}

impl<F: Read> Read for Named<F> {
    fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
        self.file.read(buf).map_err(|error| about(&self.path, error))
    }
}

impl<F: Write> Write for Named<F> {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.file.write(buf).map_err(|error| about(&self.path, error))
    }

    fn flush(&mut self) -> io::Result<()> {
        self.file.flush().map_err(|error| about(&self.path, error))
    }
}

fn decode(src: &Path, dst: &Path) -> Result<()> {
    let opened = File::open(src).map_err(|error| about(src, error))?;
    let input = BufReader::new(Named { file: opened, path: src.to_path_buf() });
    let part = part_of(dst);
    let decoded = File::create(&part).map_err(|error| Failure::from(about(&part, error))).and_then(|file| {
        let mut decoder = Decoder::new(input, BufWriter::new(Named { file, path: part.clone() }));
        decoder.header()?;
        decoder.data()?;
        decoder.out.flush()?;
        Ok(())
    });
    let done = decoded.and_then(|()| fs::rename(&part, dst).map_err(|error| Failure::from(about(dst, error))));
    if done.is_err() {
        let _ = fs::remove_file(&part);
    }
    done
}

/// The RINEX observation file of the Hatanaka-compressed file `src` (Compact RINEX 1.0, 3.0 or 3.1),
/// written to `dst` as CRX2RNX of RNXCMP 4.2.0 writes it, through `dst` + ".part", renamed when
/// complete (API 9). A file that is damaged or cut short - a line of a megabyte or more is no Compact
/// RINEX - raises ValueError("<name of src>, line <n>: <what is wrong>") and leaves neither `dst` nor
/// `dst` + ".part"; a file that cannot be read or written raises OSError with its path. Runs without
/// the GIL.
#[pyfunction]
pub fn crx2rnx(py: Python<'_>, src: &str, dst: &str) -> PyResult<()> {
    let name = Path::new(src).file_name().map_or_else(|| src.to_string(), |n| n.to_string_lossy().into_owned());
    let (src, dst) = (PathBuf::from(src), PathBuf::from(dst));
    match py.allow_threads(move || decode(&src, &dst)) {
        Ok(()) => Ok(()),
        Err(Failure::Format { line, what }) => Err(PyValueError::new_err(format!("{name}, line {line}: {what}"))),
        Err(Failure::Io(error)) => Err(error.into()),
    }
}
