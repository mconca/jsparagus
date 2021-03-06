//! Functions to exercise the parser from the command line.

use std::ffi::OsStr;
use std::fs;
use std::io;
use std::io::prelude::*; // flush() at least
use std::path::Path;

use ast::{
    self,
    types::{Program, Script},
};
use bumpalo::Bump;
use emitter;
use parser::{parse_script, ParseError};

#[derive(Clone, Debug, Default)]
pub struct DemoStats {
    files_attempted: usize,
    files_parsed: usize,

    /// Total size of all the files attempted, in bytes.
    total_bytes: u64,
}

impl DemoStats {
    pub fn new() -> DemoStats {
        DemoStats::default()
    }

    pub fn new_single(size_bytes: u64, success: bool) -> DemoStats {
        DemoStats {
            files_attempted: 1,
            files_parsed: if success { 1 } else { 0 },
            total_bytes: size_bytes,
        }
    }

    pub fn add(&mut self, other: &DemoStats) {
        self.files_attempted += other.files_attempted;
        self.files_parsed += other.files_parsed;
        self.total_bytes += other.total_bytes;
    }
}

/// Try parsing a file.
///
/// Returns an Err only if opening or reading the file fails;
/// parse errors are simply printed to stdout.
fn parse_file(path: &Path, size_bytes: u64) -> io::Result<DemoStats> {
    print!("{}:", path.display());
    io::stdout().flush()?;
    let contents = match fs::read_to_string(path) {
        Err(err) => {
            println!(" error reading file: {}", err);
            return Ok(DemoStats::new_single(size_bytes, false));
        }
        Ok(s) => s,
    };
    let allocator = &Bump::new();
    let result = parse_script(allocator, &contents);
    let stats = DemoStats::new_single(size_bytes, result.is_ok());
    match result {
        Ok(_ast) => println!(" ok"),
        Err(err) => println!(" error: {}", err.message()),
    }
    Ok(stats)
}

/// Try parsing all the files in a directory, recursively.
///
/// Returns an Err only if reading a file or directory fails;
/// parse errors are simply printed to stdout.
fn parse_dir(path: &Path) -> io::Result<DemoStats> {
    let mut summary = DemoStats::new();
    for entry_result in fs::read_dir(&path)? {
        let entry = entry_result?;
        let file = entry.path();
        let metadata = entry.metadata()?;
        let stats = if metadata.is_file() {
            parse_file(&file, metadata.len())?
        } else if metadata.is_dir() {
            parse_dir(&file)?
        } else {
            DemoStats::new()
        };
        summary.add(&stats);
    }
    Ok(summary)
}

/// Try parsing a file, or all the files in a directory recursively.
///
/// Returns an Err only if reading a file or directory fails;
/// parse errors are simply printed to stdout.
pub fn parse_file_or_dir(filename: &impl AsRef<OsStr>) -> io::Result<DemoStats> {
    let path = Path::new(filename);
    let metadata = path.metadata()?;
    if metadata.is_dir() {
        parse_dir(path)
    } else {
        // No `if metadata.is_file()` here, we instead try opening it and let
        // that fail if this is some exotic filesystem thingy. That way the
        // user gets an error message.
        parse_file(Path::new(filename), metadata.len())
    }
}

fn handle_script<'alloc>(script: Script<'alloc>) {
    println!("{:#?}", script);
    let mut program = Program::Script(script);
    match emitter::emit(&mut program) {
        Err(err) => {
            eprintln!("error: {}", err);
        }
        Ok(emit_result) => {
            println!("\n{:#?}", emit_result);
            println!("\n{}", emitter::dis(&emit_result.bytecode));

            let eval_result = interpreter::evaluate(&emit_result);
            println!("{:?}", eval_result);
        }
    }
}

pub fn read_print_loop() {
    loop {
        let allocator = &Bump::new();
        match parser::read_script_interactively(allocator, "js> ", "..> ") {
            Err(ParseError::UnexpectedEnd) => {
                println!();
                break;
            }
            Err(err) => {
                eprintln!("error: {}", err);
            }
            Ok(script) => {
                handle_script(script.unbox());
            }
        }
    }
}
