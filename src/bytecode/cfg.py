import sys
import types
from collections import defaultdict
from dataclasses import dataclass
from typing import (
    Any,
    Dict,
    Generator,
    Iterable,
    Iterator,
    List,
    Optional,
    Set,
    SupportsIndex,
    Tuple,
    TypeVar,
    Union,
    overload,
)

# alias to keep the 'bytecode' variable free
import bytecode as _bytecode
from bytecode.concrete import ConcreteInstr
from bytecode.flags import CompilerFlags
from bytecode.instr import UNSET, Instr, Label, SetLineno, TryBegin, TryEnd
from bytecode.utils import PY310, PY311, PY313

T = TypeVar("T", bound="BasicBlock")
U = TypeVar("U", bound="ControlFlowGraph")


class BasicBlock(_bytecode._InstrList[Union[Instr, SetLineno, TryBegin, TryEnd]]):
    def __init__(
        self,
        instructions: Optional[
            Iterable[Union[Instr, SetLineno, TryBegin, TryEnd]]
        ] = None,
    ) -> None:
        # a BasicBlock object, or None
        self.next_block: Optional["BasicBlock"] = None
        if instructions:
            super().__init__(instructions)

    def __iter__(self) -> Iterator[Union[Instr, SetLineno, TryBegin, TryEnd]]:
        index = 0
        while index < len(self):
            instr = self[index]
            index += 1

            if not isinstance(instr, (SetLineno, Instr, TryBegin, TryEnd)):
                raise ValueError(
                    "BasicBlock must only contain SetLineno and Instr objects, "
                    "but %s was found" % instr.__class__.__name__
                )

            if isinstance(instr, Instr) and instr.has_jump():
                if index < len(self) and any(
                    isinstance(self[i], Instr) for i in range(index, len(self))
                ):
                    raise ValueError(
                        "Only the last instruction of a basic block can be a jump"
                    )

                if not isinstance(instr.arg, BasicBlock):
                    raise ValueError(
                        "Jump target must a BasicBlock, got %s",
                        type(instr.arg).__name__,
                    )

            if isinstance(instr, TryBegin):
                if not isinstance(instr.target, BasicBlock):
                    raise ValueError(
                        "TryBegin target must a BasicBlock, got %s",
                        type(instr.target).__name__,
                    )

            yield instr

    @overload
    def __getitem__(
        self, index: SupportsIndex
    ) -> Union[Instr, SetLineno, TryBegin, TryEnd]: ...

    @overload
    def __getitem__(self: T, index: slice) -> T: ...

    def __getitem__(self, index):
        value = super().__getitem__(index)
        if isinstance(index, slice):
            value = type(self)(value)
            value.next_block = self.next_block

        return value

    def get_last_non_artificial_instruction(self) -> Optional[Instr]:
        for instr in reversed(self):
            if isinstance(instr, Instr):
                return instr

        return None

    def copy(self: T) -> T:
        new = type(self)(super().copy())
        new.next_block = self.next_block
        return new

    def legalize(self, first_lineno: int) -> int:
        """Check that all the element of the list are valid and remove SetLineno."""
        lineno_pos = []
        set_lineno = None
        current_lineno = first_lineno

        for pos, instr in enumerate(self):
            if isinstance(instr, SetLineno):
                set_lineno = current_lineno = instr.lineno
                lineno_pos.append(pos)
                continue
            if isinstance(instr, (TryBegin, TryEnd)):
                continue

            if set_lineno is not None:
                instr.lineno = set_lineno
            elif instr.lineno is UNSET:
                instr.lineno = current_lineno
            elif instr.lineno is not None:
                current_lineno = instr.lineno

        for i in reversed(lineno_pos):
            del self[i]

        return current_lineno

    def get_jump(self) -> Optional["BasicBlock"]:
        if not self:
            return None

        last_instr = self.get_last_non_artificial_instruction()
        if last_instr is None or not last_instr.has_jump():
            return None

        target_block = last_instr.arg
        assert isinstance(target_block, BasicBlock)
        return target_block

    def get_trailing_try_end(self, index: int):
        while index + 1 < len(self):
            if isinstance(b := self[index + 1], TryEnd):
                return b
            index += 1

        return None


def _update_size(pre_delta, post_delta, size, maxsize, minsize):
    size += pre_delta
    if size < 0:
        msg = "Failed to compute stacksize, got negative size"
        raise RuntimeError(msg)
    size += post_delta
    maxsize = max(maxsize, size)
    minsize = min(minsize, size)
    return size, maxsize, minsize


# We can never have nested TryBegin, so we can simply update the min stack size
# when we encounter one and use the number we have when we encounter the TryEnd


@dataclass
class _StackSizeComputationStorage:
    """Common storage shared by the computers involved in computing CFG stack usage."""

    #: Should we check that all stack operation are "safe" i.e. occurs while there
    #: is a sufficient number of items on the stack.
    check_pre_and_post: bool

    #: Id the blocks for which an analysis is under progress to avoid getting stuck
    #: in recursions.
    seen_blocks: Set[int]

    #: Sizes and exception handling status with which the analysis of the block
    #: has been performed. Used to avoid running multiple times equivalent analysis.
    blocks_startsizes: Dict[int, Set[Tuple[int, Optional[bool]]]]

    #: Track the encountered TryBegin pseudo-instruction to update their target
    #: depth at the end of the calculation.
    try_begins: List[TryBegin]

    #: Stacksize that should be used for exception blocks. This is the smallest size
    #: with which this block was reached which is the only size that can be safely
    #: restored.
    exception_block_startsize: Dict[int, int]

    #: Largest stack size used in an exception block. We record the size corresponding
    #: to the smallest start size for the block since the interpreter enforces that
    #: we start with this size.
    exception_block_maxsize: Dict[int, int]


class _StackSizeComputer:
    """Helper computing the stack usage for a single block."""

    #: Common storage shared by all helpers involved in the stack size computation
    common: _StackSizeComputationStorage

    #: Block this helper is running the computation for.
    block: BasicBlock

    #: Current stack usage.
    size: int

    #: Maximal stack usage.
    maxsize: int

    #: Minimal stack usage. This value is only relevant in between a TryBegin/TryEnd
    #: pair and determine the startsize for the exception handling block associated
    #: with the try begin.
    minsize: int

    #: Flag indicating if the block analyzed is an exception handler (i.e. a target
    #: of a TryBegin).
    exception_handler: Optional[bool]

    #: TryBegin that was encountered before jumping to this block and for which
    #: no try end was met yet.
    pending_try_begin: Optional[TryBegin]

    def __init__(
        self,
        common: _StackSizeComputationStorage,
        block: BasicBlock,
        size: int,
        maxsize: int,
        minsize: int,
        exception_handler: Optional[bool],
        pending_try_begin: Optional[TryBegin],
    ) -> None:
        self.common = common
        self.block = block
        self.size = size
        self.maxsize = maxsize
        self.minsize = minsize
        self.exception_handler = exception_handler
        self.pending_try_begin = pending_try_begin
        self._current_try_begin = pending_try_begin

    def run(self) -> Generator[Union["_StackSizeComputer", int], int, None]:
        """Iterate over the block instructions to compute stack usage."""
        # Blocks are not hashable but in this particular context we know we won't be
        # modifying blocks in place so we can safely use their id as hash rather than
        # making them generally hashable which would be weird since they are list
        # subclasses
        block_id = id(self.block)

        # If the block is currently being visited (seen = True) or
        # it was visited previously with parameters that makes the computation
        # irrelevant return the maxsize.
        fingerprint = (self.size, self.exception_handler)
        if id(self.block) in self.common.seen_blocks or (
            not self._is_stacksize_computation_relevant(block_id, fingerprint)
        ):
            yield self.maxsize

        # Prevent recursive visit of block if two blocks are nested (jump from one
        # to the other).
        self.common.seen_blocks.add(block_id)

        # Track which size has been used to run an analysis to avoid re-running multiple
        # times the same calculation.
        self.common.blocks_startsizes[block_id].add(fingerprint)

        # If this block is an exception handler reached through the exception table
        # we will push some extra objects on the stack before processing start.
        if self.exception_handler is not None:
            self._update_size(0, 1 + self.exception_handler)
            # True is used to indicated that push_lasti is True, leading to pushing
            # an extra object on the stack.

        for i, instr in enumerate(self.block):
            # Ignore SetLineno
            if isinstance(instr, (SetLineno)):
                continue

            # When we encounter a TryBegin, we:
            # - store it as the current TryBegin (since TryBegin cannot be nested)
            # - record its existence to remember to update its stack size when
            #   the computation ends
            # - update the minsize to the current size value since we need to
            #   know the minimal stack usage between the TryBegin/TryEnd pair to
            #   set the startsize of the exception handling block
            #
            # This approach does not require any special handling for with statements.
            if isinstance(instr, TryBegin):
                assert self._current_try_begin is None
                self.common.try_begins.append(instr)
                self._current_try_begin = instr
                self.minsize = self.size

                continue

            elif isinstance(instr, TryEnd):
                # When we encounter a TryEnd we can start the computation for the
                # exception block using the minimum stack size encountered since
                # the TryBegin matching this TryEnd.

                # TryBegin cannot be nested so a TryEnd should always match the
                # current try begin. However inside the CFG some blocks may
                # start with a TryEnd relevant only when reaching this block
                # through a particular jump. So we are lenient here.
                if instr.entry is not self._current_try_begin:
                    continue

                # Compute the stack usage of the exception handler
                assert isinstance(instr.entry.target, BasicBlock)
                yield from self._compute_exception_handler_stack_usage(
                    instr.entry.target,
                    instr.entry.push_lasti,
                )
                self._current_try_begin = None
                continue

            # For instructions with a jump first compute the stacksize required when the
            # jump is taken.
            if instr.has_jump():
                effect = (
                    instr.pre_and_post_stack_effect(jump=True)
                    if self.common.check_pre_and_post
                    else (instr.stack_effect(jump=True), 0)
                )
                taken_size, maxsize, minsize = _update_size(
                    *effect, self.size, self.maxsize, self.minsize
                )

                # Yield the parameters required to compute the stacksize required
                # by the block to which the jump points to and resume when we now
                # the maxsize.
                assert isinstance(instr.arg, BasicBlock)
                maxsize = yield _StackSizeComputer(
                    self.common,
                    instr.arg,
                    taken_size,
                    maxsize,
                    minsize,
                    None,
                    # Do not propagate the TryBegin if a final instruction is followed
                    # by a TryEnd.
                    None
                    if instr.is_final() and self.block.get_trailing_try_end(i)
                    else self._current_try_begin,
                )

                # Update the maximum used size by the usage implied by the following
                # the jump
                self.maxsize = max(self.maxsize, maxsize)

                # For unconditional jumps abort early since the other instruction will
                # never be seen.
                if instr.is_uncond_jump():
                    # Check for TryEnd after the final instruction which is possible
                    # TryEnd being only pseudo instructions
                    # TryBegin cannot be nested so a TryEnd should always match the
                    # current try begin. However inside the CFG some blocks may
                    # start with a TryEnd relevant only when reaching this block
                    # through a particular jump. So we are lenient here.
                    if (
                        te := self.block.get_trailing_try_end(i)
                    ) and te.entry is self._current_try_begin:
                        assert isinstance(te.entry.target, BasicBlock)
                        yield from self._compute_exception_handler_stack_usage(
                            te.entry.target,
                            te.entry.push_lasti,
                        )

                    self.common.seen_blocks.remove(id(self.block))
                    yield self.maxsize

            # jump=False: non-taken path of jumps, or any non-jump
            effect = (
                instr.pre_and_post_stack_effect(jump=False)
                if self.common.check_pre_and_post
                else (instr.stack_effect(jump=False), 0)
            )
            self._update_size(*effect)

            # Instruction is final (return, raise, ...) so any following instruction
            # in the block is dead code.
            if instr.is_final():
                # Check for TryEnd after the final instruction which is possible
                # TryEnd being only pseudo instructions.
                if te := self.block.get_trailing_try_end(i):
                    assert isinstance(te.entry.target, BasicBlock)
                    yield from self._compute_exception_handler_stack_usage(
                        te.entry.target,
                        te.entry.push_lasti,
                    )

                self.common.seen_blocks.remove(id(self.block))

                yield self.maxsize

        if self.block.next_block:
            self.maxsize = yield _StackSizeComputer(
                self.common,
                self.block.next_block,
                self.size,
                self.maxsize,
                self.minsize,
                None,
                self._current_try_begin,
            )

        self.common.seen_blocks.remove(id(self.block))

        yield self.maxsize

    # --- Private API

    _current_try_begin: Optional[TryBegin]

    def _update_size(self, pre_delta: int, post_delta: int) -> None:
        size, maxsize, minsize = _update_size(
            pre_delta, post_delta, self.size, self.maxsize, self.minsize
        )
        self.size = size
        self.minsize = minsize
        self.maxsize = maxsize

    def _compute_exception_handler_stack_usage(
        self, block: BasicBlock, push_lasti: bool
    ) -> Generator[Union["_StackSizeComputer", int], int, None]:
        b_id = id(block)
        if self.minsize < self.common.exception_block_startsize[b_id]:
            block_size = yield _StackSizeComputer(
                self.common,
                block,
                self.minsize,
                self.maxsize,
                self.minsize,
                push_lasti,
                None,
            )
            # The entry cannot be smaller than abs(stc.minimal_entry_size) as otherwise
            # we an underflow would have occured.
            self.common.exception_block_startsize[b_id] = self.minsize
            self.common.exception_block_maxsize[b_id] = block_size

    def _is_stacksize_computation_relevant(
        self, block_id: int, fingerprint: Tuple[int, Optional[bool]]
    ) -> bool:
        if PY311:
            # The computation is relevant if the block was not visited previously
            # with the same starting size and exception handler status than the
            # one in use
            return fingerprint not in self.common.blocks_startsizes[block_id]
        else:
            # The computation is relevant if the block was only visited with smaller
            # starting sizes than the one in use
            if sizes := self.common.blocks_startsizes[block_id]:
                return fingerprint[0] > max(f[0] for f in sizes)
            else:
                return True


class ControlFlowGraph(_bytecode.BaseBytecode):
    def __init__(self) -> None:
        super().__init__()
        self._blocks: List[BasicBlock] = []
        self._block_index: Dict[int, int] = {}
        self.argnames: List[str] = []

        self.add_block()

    def legalize(self) -> None:
        """Legalize all blocks."""
        current_lineno = self.first_lineno
        for block in self._blocks:
            current_lineno = block.legalize(current_lineno)

    def get_block_index(self, block: BasicBlock) -> int:
        try:
            return self._block_index[id(block)]
        except KeyError:
            raise ValueError(f"the block {block} is not part of this bytecode")  # noqa

    def _add_block(self, block: BasicBlock) -> None:
        block_index = len(self._blocks)
        self._blocks.append(block)
        self._block_index[id(block)] = block_index

    def add_block(
        self, instructions: Optional[Iterable[Union[Instr, SetLineno]]] = None
    ) -> BasicBlock:
        block = BasicBlock(instructions)
        self._add_block(block)
        return block

    def compute_stacksize(
        self,
        *,
        check_pre_and_post: bool = True,
        compute_exception_stack_depths: bool = True,
    ) -> int:
        """Compute the stack size by iterating through the blocks

        The implementation make use of a generator function to avoid issue with
        deeply nested recursions.

        """
        # In the absence of any block return 0
        if not self:
            return 0

        # Create the common storage for the calculation
        common = _StackSizeComputationStorage(
            check_pre_and_post,
            seen_blocks=set(),
            blocks_startsizes={id(b): set() for b in self},
            exception_block_startsize=dict.fromkeys([id(b) for b in self], 32768),
            exception_block_maxsize=dict.fromkeys([id(b) for b in self], -32768),
            try_begins=[],
        )

        # Starting with Python 3.10, generator and coroutines start with one object
        # on the stack (None, anything is an error).
        initial_stack_size = 0
        if (
            not PY313  # under 3.13+ RETURN_GENERATOR make this explicit
            and PY310
            and self.flags
            & (
                CompilerFlags.GENERATOR
                | CompilerFlags.COROUTINE
                | CompilerFlags.ASYNC_GENERATOR
            )
        ):
            initial_stack_size = 1

        # Create a generator/coroutine responsible of dealing with the first block
        coro = _StackSizeComputer(
            common, self[0], initial_stack_size, 0, 0, None, None
        ).run()

        # Create a list of generator that have not yet been exhausted
        coroutines: List[Generator[Union[_StackSizeComputer, int], int, None]] = []

        push_coroutine = coroutines.append
        pop_coroutine = coroutines.pop
        args = None

        try:
            while True:
                # Mypy does not seem to honor the fact that one must send None
                # to a brand new generator irrespective of its send type.
                args = coro.send(None)  # type: ignore

                # Consume the stored generators as long as they return a simple
                # integer that is to be used to resume the last stored generator.
                while isinstance(args, int):
                    coro = pop_coroutine()
                    args = coro.send(args)

                # Otherwise we enter a new block and we store the generator under
                # use and create a new one to process the new block
                push_coroutine(coro)
                coro = args.run()

        except IndexError:
            # The exception occurs when all the generators have been exhausted
            # in which case the last yielded value is the stacksize.
            assert args is not None and isinstance(args, int)

            # Exception handling block size is reported separately since we need
            # to report only the stack usage for the smallest start size for the
            # block
            args = max(args, *common.exception_block_maxsize.values())

            # Check if there is dead code that may contain TryBegin/TryEnd pairs.
            # For any such pair we set a huge size (the exception table format does not
            # mandate a maximum value). We do so so that if  the pair is fused with
            # another it does not alter the computed size.
            for block in self:
                if not common.blocks_startsizes[id(block)]:
                    for i in block:
                        if isinstance(i, TryBegin) and i.stack_depth is UNSET:
                            i.stack_depth = 32768

            # If requested update the TryBegin stack size
            if compute_exception_stack_depths:
                for tb in common.try_begins:
                    size = common.exception_block_startsize[id(tb.target)]
                    assert size >= 0
                    tb.stack_depth = size

            return args

    def __repr__(self) -> str:
        return "<ControlFlowGraph block#=%s>" % len(self._blocks)

    # Helper to obtain a flat list of instr, which does not refer to block at
    # anymore. Used for comparison of different CFG.
    def _get_instructions(
        self,
    ) -> List:
        instructions: List = []
        try_begins: Dict[TryBegin, int] = {}

        for block in self:
            for index, instr in enumerate(block):
                if isinstance(instr, TryBegin):
                    assert isinstance(instr.target, BasicBlock)
                    try_begins.setdefault(instr, len(try_begins))
                    instructions.append(
                        (
                            "TryBegin",
                            try_begins[instr],
                            self.get_block_index(instr.target),
                            instr.push_lasti,
                        )
                    )
                elif isinstance(instr, TryEnd):
                    instructions.append(("TryEnd", try_begins[instr.entry]))
                elif isinstance(instr, Instr) and (
                    instr.has_jump() or instr.is_final()
                ):
                    if instr.has_jump():
                        target_block = instr.arg
                        assert isinstance(target_block, BasicBlock)
                        # We use a concrete instr here to be able to use an integer as
                        # argument rather than a Label. This is fine for comparison
                        # purposes which is our sole goal here.
                        c_instr = ConcreteInstr(
                            instr.name,
                            self.get_block_index(target_block),
                            location=instr.location,
                        )
                        instructions.append(c_instr)
                    else:
                        instructions.append(instr)

                    if te := block.get_trailing_try_end(index):
                        instructions.append(("TryEnd", try_begins[te.entry]))
                    break
                else:
                    instructions.append(instr)

        return instructions

    def __eq__(self, other: Any) -> bool:
        if type(self) is not type(other):
            return False

        if self.argnames != other.argnames:
            return False

        instrs1 = self._get_instructions()
        instrs2 = other._get_instructions()
        if instrs1 != instrs2:
            return False
        # FIXME: compare block.next_block

        return super().__eq__(other)

    def __len__(self) -> int:
        return len(self._blocks)

    def __iter__(self) -> Iterator[BasicBlock]:
        return iter(self._blocks)

    @overload
    def __getitem__(self, index: Union[int, BasicBlock]) -> BasicBlock: ...

    @overload
    def __getitem__(self: U, index: slice) -> U: ...

    def __getitem__(self, index):
        if isinstance(index, BasicBlock):
            index = self.get_block_index(index)
        return self._blocks[index]

    def __delitem__(self, index: Union[int, BasicBlock]) -> None:
        if isinstance(index, BasicBlock):
            index = self.get_block_index(index)
        block = self._blocks[index]
        del self._blocks[index]
        del self._block_index[id(block)]
        for i in range(index, len(self)):
            block = self._blocks[i]
            self._block_index[id(block)] -= 1

    def split_block(self, block: BasicBlock, index: int) -> BasicBlock:
        if not isinstance(block, BasicBlock):
            raise TypeError("expected block")
        block_index = self.get_block_index(block)

        if index < 0:
            raise ValueError("index must be positive")

        block = self._blocks[block_index]
        if index == 0:
            return block

        if index > len(block):
            raise ValueError("index out of the block")

        instructions = block[index:]
        if not instructions:
            if block_index + 1 < len(self):
                return self[block_index + 1]

        del block[index:]

        block2 = BasicBlock(instructions)
        block.next_block = block2

        for block in self[block_index + 1 :]:
            self._block_index[id(block)] += 1

        self._blocks.insert(block_index + 1, block2)
        self._block_index[id(block2)] = block_index + 1

        return block2

    def get_dead_blocks(self) -> List[BasicBlock]:
        if not self:
            return []

        seen_block_ids = set()
        stack = [self[0]]
        while stack:
            block = stack.pop()
            if id(block) in seen_block_ids:
                continue
            seen_block_ids.add(id(block))
            fall_through = True
            for i in block:
                if isinstance(i, Instr):
                    if isinstance(i.arg, BasicBlock):
                        stack.append(i.arg)
                    if i.is_final():
                        fall_through = False
                elif isinstance(i, TryBegin):
                    assert isinstance(i.target, BasicBlock)
                    stack.append(i.target)
            if fall_through and block.next_block:
                stack.append(block.next_block)

        return [b for b in self if id(b) not in seen_block_ids]

    @staticmethod
    def from_bytecode(bytecode: _bytecode.Bytecode) -> "ControlFlowGraph":
        # label => instruction index
        label_to_block_index = {}
        jumps = []
        try_end_locations = {}
        for index, instr in enumerate(bytecode):
            if isinstance(instr, Label):
                label_to_block_index[instr] = index
            elif isinstance(instr, Instr) and isinstance(instr.arg, Label):
                jumps.append((index, instr.arg))
            elif isinstance(instr, TryBegin):
                assert isinstance(instr.target, Label)
                jumps.append((index, instr.target))
            elif isinstance(instr, TryEnd):
                try_end_locations[instr.entry] = index

        # Figure out on which index block targeted by a label start
        block_starts = {}
        for target_index, target_label in jumps:
            target_index = label_to_block_index[target_label]
            block_starts[target_index] = target_label

        bytecode_blocks = ControlFlowGraph()
        bytecode_blocks._copy_attr_from(bytecode)
        bytecode_blocks.argnames = list(bytecode.argnames)

        # copy instructions, convert labels to block labels
        block = bytecode_blocks[0]
        labels = {}
        jumping_instrs: List[Instr] = []
        # Map input TryBegin to CFG TryBegins (split across blocks may yield multiple
        # TryBegin from a single in the bytecode).
        try_begins: Dict[TryBegin, list[TryBegin]] = {}
        # Storage for TryEnds that need to be inserted at the beginning of a block.
        # We use a list because the same block can be reached through several paths
        # with different active TryBegins
        add_try_end: Dict[Label, List[TryEnd]] = defaultdict(list)

        # Track the currently active try begin
        active_try_begin: Optional[TryBegin] = None
        try_begin_inserted_in_block = False
        last_instr: Optional[Instr] = None
        for index, instr in enumerate(bytecode):
            # Reference to the current block if we create a new one in the following.
            old_block: BasicBlock | None = None

            # First we determine if we need to create a new block:
            # - by checking the current instruction index
            if index in block_starts:
                old_label = block_starts[index]
                # Create a new block if the last created one is not empty
                # (of real instructions)
                if index != 0 and (li := block.get_last_non_artificial_instruction()):
                    old_block = block
                    new_block = bytecode_blocks.add_block()
                    # If the last non artificial instruction is not final connect
                    # this block to the next.
                    if not li.is_final():
                        block.next_block = new_block
                    block = new_block
                if old_label is not None:
                    labels[old_label] = block

            # - by inspecting the last instr
            elif block.get_last_non_artificial_instruction() and last_instr is not None:
                # The last instruction is final but we did not create a block
                # -> sounds like a block of dead code but we preserve it
                if last_instr.is_final():
                    old_block = block
                    block = bytecode_blocks.add_block()

                # We are dealing with a conditional jump
                elif last_instr.has_jump():
                    assert isinstance(last_instr.arg, Label)
                    old_block = block
                    new_block = bytecode_blocks.add_block()
                    block.next_block = new_block
                    block = new_block

            # If we created a new block, we check:
            # - if the current instruction is a TryEnd and if the last instruction
            #   is final in which case we insert the TryEnd in the old block.
            # - if we have a currently active TryBegin for which we may need to
            #   create a TryEnd in the previous block and a new TryBegin in the
            #   new one because the blocks are not connected.
            if old_block is not None:
                temp = try_begin_inserted_in_block
                try_begin_inserted_in_block = False

            if old_block is not None and last_instr is not None:
                # The last instruction is final, if the current instruction is a
                # TryEnd insert it in the same block and move to the next instruction
                if last_instr.is_final() and isinstance(instr, TryEnd):
                    assert active_try_begin
                    nte = instr.copy()
                    nte.entry = try_begins[active_try_begin][-1]
                    old_block.append(nte)
                    active_try_begin = None
                    continue

                # If we have an active TryBegin and last_instr is:
                elif active_try_begin is not None:
                    # - a jump whose target is beyond the TryEnd of the active
                    #   TryBegin: we remember TryEnd should be prepended to the
                    #   target block.
                    if (
                        last_instr.has_jump()
                        and active_try_begin in try_end_locations
                        and (
                            # last_instr is a jump so arg is a Label
                            label_to_block_index[last_instr.arg]  # type: ignore
                            >= try_end_locations[active_try_begin]
                        )
                    ):
                        assert isinstance(last_instr.arg, Label)
                        add_try_end[last_instr.arg].append(
                            TryEnd(try_begins[active_try_begin][-1])
                        )

                    # - final and the try begin originate from the current block:
                    #   we insert a TryEnd in the old block and a new TryBegin in
                    #   the new one since the blocks are disconnected.
                    if last_instr.is_final() and temp:
                        old_block.append(TryEnd(try_begins[active_try_begin][-1]))
                        new_tb = active_try_begin.copy()
                        block.append(new_tb)
                        # Add this new TryBegin to the map to properly update
                        # the target.
                        try_begins[active_try_begin].append(new_tb)
                        try_begin_inserted_in_block = True

            last_instr = None

            if isinstance(instr, Label):
                continue

            # don't copy SetLineno objects
            if isinstance(instr, (Instr, TryBegin, TryEnd)):
                new = instr.copy()
                if isinstance(instr, TryBegin):
                    assert active_try_begin is None
                    active_try_begin = instr
                    try_begin_inserted_in_block = True
                    assert isinstance(new, TryBegin)
                    try_begins[instr] = [new]
                elif isinstance(instr, TryEnd):
                    assert isinstance(new, TryEnd)
                    new.entry = try_begins[instr.entry][-1]
                    active_try_begin = None
                    try_begin_inserted_in_block = False
                else:
                    last_instr = instr
                    if isinstance(instr.arg, Label):
                        assert isinstance(new, Instr)
                        jumping_instrs.append(new)

                instr = new

            block.append(instr)

        # Insert the necessary TryEnds at the beginning of block that were marked
        # (if we did not already insert an equivalent TryEnd earlier).
        for lab, tes in add_try_end.items():
            block = labels[lab]
            existing_te_entries = set()
            index = 0
            # We use a while loop since the block cannot yet be iterated on since
            # jumps still use labels instead of blocks
            while index < len(block):
                i = block[index]
                index += 1
                if isinstance(i, TryEnd):
                    existing_te_entries.add(i.entry)
                else:
                    break
            for te in tes:
                if te.entry not in existing_te_entries:
                    labels[lab].insert(0, te)
                    existing_te_entries.add(te.entry)

        # Replace labels by block in jumping instructions
        for instr in jumping_instrs:
            label = instr.arg
            assert isinstance(label, Label)
            instr.arg = labels[label]

        # Replace labels by block in TryBegin
        for b_tb, c_tbs in try_begins.items():
            label = b_tb.target
            assert isinstance(label, Label)
            for c_tb in c_tbs:
                c_tb.target = labels[label]

        return bytecode_blocks

    def to_bytecode(self) -> _bytecode.Bytecode:
        """Convert to Bytecode."""

        used_blocks = set()
        for block in self:
            target_block = block.get_jump()
            if target_block is not None:
                used_blocks.add(id(target_block))

            for tb in (i for i in block if isinstance(i, TryBegin)):
                used_blocks.add(id(tb.target))

        labels = {}
        jumps = []
        try_begins = {}
        seen_try_end: Set[TryBegin] = set()
        instructions: List[Union[Instr, Label, TryBegin, TryEnd, SetLineno]] = []

        # Track the last seen TryBegin and TryEnd to be able to fuse adjacent
        # TryEnd/TryBegin pair which share the same target.
        # In each case, we store the value found in the CFG and the value
        # inserted in the bytecode.
        last_try_begin: Tuple[TryBegin, TryBegin] | None = None
        last_try_end: Tuple[TryEnd, TryEnd] | None = None

        for block in self:
            if id(block) in used_blocks:
                new_label = Label()
                labels[id(block)] = new_label
                instructions.append(new_label)

            for instr in block:
                # don't copy SetLineno objects
                if isinstance(instr, (Instr, TryBegin, TryEnd)):
                    new = instr.copy()
                    if isinstance(instr, TryBegin):
                        # If due to jumps and split TryBegin, we encounter a TryBegin
                        # while we still have a TryBegin ensure they can be fused.
                        if last_try_begin is not None:
                            cfg_tb, byt_tb = last_try_begin
                            assert instr.target is cfg_tb.target
                            assert instr.push_lasti == cfg_tb.push_lasti
                            byt_tb.stack_depth = min(
                                byt_tb.stack_depth, instr.stack_depth
                            )

                        # If the TryBegin share the target and push_lasti of the
                        # entry of an adjacent TryEnd, omit the new TryBegin that
                        # was inserted to allow analysis of the CFG and remove
                        # the already inserted TryEnd.
                        if last_try_end is not None:
                            cfg_te, byt_te = last_try_end
                            entry = cfg_te.entry
                            if (
                                entry.target is instr.target
                                and entry.push_lasti == instr.push_lasti
                            ):
                                # If we did not yet compute the required stack depth
                                # keep the value as UNSET
                                if entry.stack_depth is UNSET:
                                    assert instr.stack_depth is UNSET
                                    byt_te.entry.stack_depth = UNSET
                                else:
                                    byt_te.entry.stack_depth = min(
                                        entry.stack_depth, instr.stack_depth
                                    )
                                try_begins[instr] = byt_te.entry
                                instructions.remove(byt_te)
                                continue
                        assert isinstance(new, TryBegin)
                        try_begins[instr] = new
                        last_try_begin = (instr, new)
                        last_try_end = None
                    elif isinstance(instr, TryEnd):
                        # Only keep the first seen TryEnd matching a TryBegin
                        assert isinstance(new, TryEnd)
                        if instr.entry in seen_try_end:
                            continue
                        seen_try_end.add(instr.entry)
                        new.entry = try_begins[instr.entry]
                        last_try_begin = None
                        last_try_end = (instr, new)
                    elif isinstance(instr.arg, BasicBlock):
                        assert isinstance(new, Instr)
                        jumps.append(new)
                        last_try_end = None
                    else:
                        last_try_end = None

                    instr = new

                instructions.append(instr)

        # Map to new labels
        for instr in jumps:
            instr.arg = labels[id(instr.arg)]

        for tb in set(try_begins.values()):
            tb.target = labels[id(tb.target)]

        bytecode = _bytecode.Bytecode()
        bytecode._copy_attr_from(self)
        bytecode.argnames = list(self.argnames)
        bytecode[:] = instructions

        return bytecode

    def to_code(
        self,
        stacksize: Optional[int] = None,
        *,
        check_pre_and_post: bool = True,
        compute_exception_stack_depths: bool = True,
    ) -> types.CodeType:
        """Convert to code."""
        if stacksize is None:
            stacksize = self.compute_stacksize(
                check_pre_and_post=check_pre_and_post,
                compute_exception_stack_depths=compute_exception_stack_depths,
            )
        bc = self.to_bytecode()
        return bc.to_code(
            stacksize=stacksize,
            check_pre_and_post=False,
            compute_exception_stack_depths=False,
        )
