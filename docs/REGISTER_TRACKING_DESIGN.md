# Register Allocation Design for uplm80

## Background: Register Allocation Theory

Register allocation is a well-studied compiler problem. Common approaches include:

1. **Graph Coloring** (Chaitin): Build an interference graph where nodes are live ranges and edges connect simultaneously-live variables. Color the graph with k colors (k = number of registers). NP-complete but produces optimal results.

2. **Linear Scan**: Process live intervals in order, assigning registers greedily. O(n log n) complexity. Used by JIT compilers (V8, HotSpot, ART) where compile time matters.

3. **Sethi-Ullman**: Specifically for expression trees. Labels each node with minimum registers needed, then generates code processing the more expensive subtree first.

For uplm80, we need a simpler approach because:
- We generate code in a single pass (no separate allocation phase)
- Expression evaluation is stack-based with registers as a "register stack"
- The Z80 has very few registers with specific roles

## Z80 Register Conventions

| Register | Primary Use | Notes |
|----------|-------------|-------|
| A | BYTE values, accumulator | Most 8-bit ops require A |
| HL | ADDRESS values, primary 16-bit | Memory access via (HL), arithmetic |
| DE | Secondary 16-bit operand | Used for binary ops, block moves |
| BC | Loop counter, tertiary 16-bit | DJNZ uses B, LDIR uses BC |
| IX | Local variable frame pointer | Indexed addressing (IX+d) |
| SP | Stack pointer | Implicit in PUSH/POP/CALL/RET |

## Current Problems

The current codegen uses ad-hoc register management:

```python
# Must manually check if expression will clobber DE
source_preserves_de = self._expr_preserves_de(args[1])
if source_preserves_de:
    # Safe to use DE
else:
    # Must push/pop around the expression
```

Problems:
1. **Fragile**: `_expr_preserves_de` must know internals of all expression types
2. **Error-prone**: Easy to miss cases (caused the MOVE bug)
3. **Hard to extend**: New expression types require updating multiple places
4. **No optimization**: Can't reorder to minimize spills

## Proposed Design: Register Descriptors with Demand-Driven Allocation

### Core Concept

Each register has a **descriptor** tracking:
- **State**: `free`, `busy`, or `spilled`
- **Contents**: What value it holds (for debugging/optimization)
- **Spill location**: Stack offset if spilled

When code needs a register:
1. **Request** a specific register (e.g., "need HL for result") or a class (e.g., "need any 16-bit")
2. If register is **free**: mark busy, return it
3. If register is **busy**: automatically **spill** to stack, mark as spilled-but-claimed
4. When done: **release** the register (restore from stack if spilled)

### Data Structures

```python
from enum import Enum, auto
from dataclasses import dataclass, field

class RegState(Enum):
    FREE = auto()      # Available for use
    BUSY = auto()      # Contains live value
    SPILLED = auto()   # Value saved to stack, register reused

class RegClass(Enum):
    """Register classes for allocation requests."""
    BYTE = auto()      # Need A
    ADDR = auto()      # Need HL
    ADDR_ALT = auto()  # Need DE or BC (secondary 16-bit)
    INDEX = auto()     # Need IX or IY

@dataclass
class RegDescriptor:
    state: RegState = RegState.FREE
    owner: str = ""           # Debug: what claimed this register
    spill_depth: int = 0      # Stack depth when spilled (for nested spills)
    contents: str = ""        # Debug: description of contents

@dataclass
class RegisterAllocator:
    """Tracks register state and manages allocation."""

    # Register descriptors
    a: RegDescriptor = field(default_factory=RegDescriptor)
    hl: RegDescriptor = field(default_factory=RegDescriptor)
    de: RegDescriptor = field(default_factory=RegDescriptor)
    bc: RegDescriptor = field(default_factory=RegDescriptor)
    ix: RegDescriptor = field(default_factory=RegDescriptor)

    # Stack tracking
    spill_stack: list[str] = field(default_factory=list)  # Order of spilled regs

    def get_reg(self, name: str) -> RegDescriptor:
        """Get descriptor by name."""
        return getattr(self, name.lower())
```

### Core Operations

#### `need_reg(reg_or_class, owner, emit_fn)` - Request a Register

```python
def need_reg(self, reg_or_class: str | RegClass, owner: str,
             emit_fn: Callable[[str, str], None]) -> str:
    """
    Request a register. Returns the register name.
    If busy, automatically spills it first.

    Args:
        reg_or_class: Specific register name ('hl', 'de') or RegClass
        owner: Debug string identifying the requester
        emit_fn: Callback to emit assembly (emit_fn('push', 'hl'))

    Returns:
        The allocated register name
    """
    # Resolve class to specific register
    if isinstance(reg_or_class, RegClass):
        reg = self._pick_reg_from_class(reg_or_class)
    else:
        reg = reg_or_class.lower()

    desc = self.get_reg(reg)

    if desc.state == RegState.BUSY:
        # Must spill - save current contents to stack
        emit_fn("push", reg)
        self.spill_stack.append(reg)
        desc.spill_depth = len(self.spill_stack)
        desc.state = RegState.SPILLED

    # Mark as busy with new owner
    desc.state = RegState.BUSY
    desc.owner = owner
    return reg

def _pick_reg_from_class(self, cls: RegClass) -> str:
    """Pick best register from class, preferring free ones."""
    candidates = {
        RegClass.BYTE: ['a'],
        RegClass.ADDR: ['hl'],
        RegClass.ADDR_ALT: ['de', 'bc'],
        RegClass.INDEX: ['ix'],
    }

    for reg in candidates[cls]:
        if self.get_reg(reg).state == RegState.FREE:
            return reg

    # All busy - return first (will be spilled)
    return candidates[cls][0]
```

#### `release_reg(reg, emit_fn)` - Release a Register

```python
def release_reg(self, reg: str, emit_fn: Callable[[str, str], None]) -> None:
    """
    Release a register. If it was spilled, restore it.

    Args:
        reg: Register name to release
        emit_fn: Callback to emit assembly
    """
    reg = reg.lower()
    desc = self.get_reg(reg)

    if desc.state == RegState.SPILLED and desc.spill_depth > 0:
        # Need to restore - but must pop in correct order
        # If this isn't top of spill stack, we have a problem
        if self.spill_stack and self.spill_stack[-1] == reg:
            emit_fn("pop", reg)
            self.spill_stack.pop()

    desc.state = RegState.FREE
    desc.owner = ""
    desc.spill_depth = 0
```

#### `with_reg(reg, owner)` - Context Manager for Scoped Use

```python
@contextmanager
def with_reg(self, reg: str, owner: str, emit_fn):
    """Context manager for scoped register use."""
    self.need_reg(reg, owner, emit_fn)
    try:
        yield reg
    finally:
        self.release_reg(reg, emit_fn)
```

### Expression Evaluation Model

The key insight is that `_gen_expr` returns results in a **known location**:
- BYTE expressions → result in **A**
- ADDRESS expressions → result in **HL**

When evaluating binary expressions like `left + right`:
1. Evaluate `left` → result in HL
2. **Claim DE** (may spill if busy)
3. `ex de,hl` → left now in DE
4. Evaluate `right` → result in HL (this may use/clobber other regs)
5. Perform `add hl,de`
6. **Release DE** (restores if spilled)

```python
def _gen_binary_expr_new(self, expr: BinaryExpr) -> DataType:
    """Generate binary expression with automatic register management."""

    # Evaluate left operand → result in HL (or A for BYTE)
    left_type = self._gen_expr(expr.left)

    if left_type == DataType.ADDRESS:
        # Need to preserve left in DE while evaluating right
        with self.regs.with_reg('de', 'binary_left', self._emit):
            self._emit("ex", "de,hl")  # DE = left
            right_type = self._gen_expr(expr.right)  # HL = right
            # Now: DE = left, HL = right
            # Perform operation...
            self._emit("add", "hl,de")  # Example: HL = left + right
    else:
        # BYTE operation - similar pattern with A and B
        ...

    return result_type
```

### Handling Nested Spills

Consider: `(a + b) + (c + d)` where we only have HL and DE:

1. Eval `a` → HL
2. Claim DE, `ex de,hl` → DE=a
3. Eval `b` → HL
4. `add hl,de` → HL = a+b
5. **Need DE again** for outer `+`, but we're releasing it
6. Claim DE, `ex de,hl` → DE = a+b
7. Eval `c+d`:
   - Eval `c` → HL
   - **Claim DE** - but it's busy! **Spill**: `push de`
   - `ex de,hl` → DE=c
   - Eval `d` → HL
   - `add hl,de` → HL = c+d
   - **Release DE** - was spilled, so `pop de` → DE = a+b restored
8. `add hl,de` → HL = (a+b) + (c+d)
9. Release DE

The spill stack ensures correct restore order.

### Sethi-Ullman Optimization (Future)

To minimize spills, we can label expression trees with register requirements:

```python
def _label_reg_need(self, expr: Expr) -> int:
    """Label expression with minimum registers needed (Sethi-Ullman)."""
    if isinstance(expr, (NumberLiteral, Identifier)):
        return 1  # Leaf: needs 1 register to hold result

    if isinstance(expr, BinaryExpr):
        left_need = self._label_reg_need(expr.left)
        right_need = self._label_reg_need(expr.right)

        if left_need == right_need:
            return left_need + 1  # Need extra reg to hold one side
        else:
            return max(left_need, right_need)  # Eval harder side first

    return 1
```

Then evaluate the subtree with **higher** register need **first** - this minimizes spills.

### Migration Strategy

#### Phase 1: Infrastructure
- Add `RegisterAllocator` class
- Add `self.regs` to `CodeGenerator.__init__`
- Keep all existing code working

#### Phase 2: Instrument Existing Code
- Add assertions to verify register state assumptions
- Log register operations to find patterns

#### Phase 3: Migrate Binary Expressions
- Update `_gen_binary_expr` to use `need_reg`/`release_reg`
- Remove `_expr_preserves_de` checks

#### Phase 4: Migrate All Expression Types
- CallExpr (function calls may clobber registers)
- SubscriptExpr (array access needs temp regs)
- MemberExpr (structure member access)

#### Phase 5: Optimize
- Implement Sethi-Ullman labeling
- Add peephole patterns to eliminate redundant push/pop

### Testing Strategy

1. **Regression tests**: All existing tests must pass
2. **Stack balance tests**: Verify SP is same before/after expressions
3. **Register state assertions**: Check registers are FREE at statement boundaries
4. **Stress tests**: Deeply nested expressions to test spill/restore

## References

- [Register Allocation - Wikipedia](https://en.wikipedia.org/wiki/Register_allocation)
- [Linear Scan Register Allocation (Poletto & Sarkar)](https://web.cs.ucla.edu/~palsberg/course/cs132/linearscan.pdf)
- [Register Allocation via Graph Coloring (Chaitin)](https://dl.acm.org/doi/10.1145/872726.806984)
- [CS701 Lecture Notes - Register Allocation](https://pages.cs.wisc.edu/~horwitz/CS701-NOTES/5.REGISTER-ALLOCATION.html)
- [Register Allocation Algorithms - GeeksforGeeks](https://www.geeksforgeeks.org/register-allocation-algorithms-in-compiler-design/)
