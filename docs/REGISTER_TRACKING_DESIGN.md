# Register Tracking Refactor Design

## Current Problem

The code generator currently uses ad-hoc approaches to manage register conflicts. Each function must know what registers other functions might clobber. For example:

```python
def _expr_preserves_de(self, expr: Expr) -> bool:
    """Check if evaluating expr will preserve the DE register."""
    # Has to know implementation details of _gen_expr...
```

This is fragile because:
- Adding new expression types requires updating all callers
- Each call site has to manually check and save/restore registers
- Easy to miss cases, causing subtle bugs (like the MOVE bug where complex source expressions clobbered DE holding the destination)

## Proposed Solution: Automatic Register Tracking

Implement a register allocation system where:
1. Registers are tracked as **free** or **busy**
2. When code needs a register that's busy, it's **automatically saved**
3. When the scope ends, busy registers are **automatically restored**
4. Functions don't need to know what other functions do

### Data Structure

```python
class RegState:
    """State of a single register."""
    busy: bool = False       # Is this register currently in use?
    saved: bool = False      # Did we push it to save its value?
    owner: str | None = None # What's using it (for debugging)

class CodeGenerator:
    def __init__(self):
        # Register tracking
        self.regs = {
            'a': RegState(),
            'hl': RegState(),
            'de': RegState(),
            'bc': RegState(),
        }
        self.reg_save_stack: list[dict[str, bool]] = []  # Stack of save states
```

### Core Operations

#### `_claim_reg(reg, owner=None)`
Claim exclusive use of a register. If busy, automatically saves it.

```python
def _claim_reg(self, reg: str, owner: str | None = None) -> None:
    """Claim a register for exclusive use. Saves if busy."""
    state = self.regs[reg]
    if state.busy and not state.saved:
        self._emit("push", reg)
        state.saved = True
    state.busy = True
    state.owner = owner
```

#### `_release_reg(reg)`
Release a register. Restores if we saved it.

```python
def _release_reg(self, reg: str) -> None:
    """Release a register. Restores if we saved it."""
    state = self.regs[reg]
    if state.saved:
        self._emit("pop", reg)
        state.saved = False
    state.busy = False
    state.owner = None
```

#### `_push_reg_context()` / `_pop_reg_context()`
Scope-based save/restore for function calls and nested expressions.

```python
def _push_reg_context(self) -> None:
    """Save current register state before entering a new scope."""
    self.reg_save_stack.append({
        reg: state.saved for reg, state in self.regs.items()
    })

def _pop_reg_context(self) -> None:
    """Restore register state when leaving a scope."""
    saved_state = self.reg_save_stack.pop()
    # Restore any registers that were saved in this context
    for reg, was_saved in reversed(saved_state.items()):
        if self.regs[reg].saved and not was_saved:
            self._emit("pop", reg)
            self.regs[reg].saved = was_saved
```

### Usage Examples

#### Before (ad-hoc approach):
```python
def _gen_builtin_move(self, args):
    # Must manually check if source preserves DE
    source_preserves_de = self._expr_preserves_de(args[1])
    if source_preserves_de:
        self._gen_expr(args[2])  # dest -> HL
        self._emit("ex", "de,hl")
        self._gen_expr(args[1])  # source -> HL
    else:
        self._gen_expr(args[2])  # dest -> HL
        self._emit("push", "hl")  # manually save
        self._gen_expr(args[1])  # source -> HL
        self._emit("pop", "de")  # manually restore to DE
```

#### After (automatic tracking):
```python
def _gen_builtin_move(self, args):
    self._gen_expr(args[2])  # dest -> HL
    self._claim_reg('de', 'move_dest')
    self._emit("ex", "de,hl")  # DE now holds dest
    self._gen_expr(args[1])   # source -> HL (auto-saves DE if needed)
    self._release_reg('de')   # auto-restores if saved
    # ... rest of MOVE
```

### Implementation Notes

1. **Start simple**: Begin with HL and DE tracking, which are the main sources of conflicts
2. **Incremental adoption**: Can coexist with existing code during migration
3. **Debug support**: The `owner` field helps debug which code claimed a register
4. **Performance**: May generate slightly more push/pop than hand-optimized code, but correctness is more important

### Migration Plan

1. Add the register tracking infrastructure (data structures, claim/release functions)
2. Modify `_gen_expr` to use claim/release for HL
3. Update call sites that use DE to use claim/release
4. Remove ad-hoc functions like `_expr_preserves_de`
5. Add assertions to catch register misuse during development

### Deferred

This refactor is deferred until after mpm2 release. The current ad-hoc fix for MOVE is sufficient for immediate needs.
