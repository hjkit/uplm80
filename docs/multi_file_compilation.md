# Multi-File Compilation with uplm80

## Overview

uplm80 supports compiling multiple PL/M source files together in a single compilation unit. This enables:
- Global optimizations across all modules
- Shared ??AUTO temporary storage
- Consistent calling conventions

## Usage

```bash
uplm80 file1.plm file2.plm file3.plm -o output.mac
```

Files are processed in order, so definitions must come before uses.

## Important: Procedure Declarations

**DO NOT use `external` for procedure declarations when compiling files together.**

The compiler uses different calling conventions:
- `external` procedures → stack-based calling (push args, call, pop)
- Defined procedures → register-based calling (??AUTO locations)

When you declare a procedure as `external` in one file, but it's defined in another file being compiled together, the calling convention mismatch will cause incorrect behavior.

### Correct Pattern

```plm
/* file1.plm - defines the procedure */
myproc: procedure(x, y) byte public;
    declare (x, y) address;
    /* ... */
end myproc;

/* file2.plm - just calls it, no declaration needed */
/* The procedure is visible because file1.plm is compiled first */
result = myproc(1, 2);
```

### Incorrect Pattern (DO NOT USE)

```plm
/* file2.plm - WRONG: external declaration */
myproc: procedure(x, y) byte external;
    declare (x, y) address;
end myproc;

/* This will use stack-based calling, but myproc expects register-based */
result = myproc(1, 2);  /* BROKEN! */
```

## Variables

Variables CAN use `external` and `public` to share storage across files:

```plm
/* file1.plm */
declare counter byte public;

/* file2.plm */
declare counter byte external;
counter = counter + 1;  /* Works correctly */
```

## Heap Allocation

The linker provides `__END__` at the end of all code/data. Since PL/M can't directly reference symbols with underscores, create a bridge in assembly:

```asm
; heap.asm
    .Z80
    PUBLIC  HEAPBASE
    EXTRN   __END__

HEAPBASE:
    DW  __END__

    END
```

Then in PL/M:

```plm
declare heap$base address external;

/* In initialization: */
buffer$ptr = heap$base;
```

Link with: `ul80 -o program.com program.rel heap.rel`

## Recommended File Order

1. **Startup module** (entry point, calls main)
2. **Common declarations** (globals, BDOS interface)
3. **Library modules** (I/O, utilities)
4. **Feature modules** (decompressors, etc.)
5. **Main module** (main procedure, program logic)

## Example Build

```makefile
SRCS = startup.plm common.plm io.plm feature.plm main.plm

$(TARGET): $(SRCS)
    uplm80 $(SRCS) -o program.mac
    um80 program.mac -o program.rel
    ul80 -o program.com program.rel
```
