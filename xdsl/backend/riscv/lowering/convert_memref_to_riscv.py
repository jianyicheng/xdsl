from collections.abc import Iterable
from math import prod
from typing import Any, cast

from xdsl.backend.riscv.lowering.utils import (
    cast_operands_to_regs,
    register_type_for_type,
)
from xdsl.builder import ImplicitBuilder
from xdsl.context import MLContext
from xdsl.dialects import memref, riscv, riscv_func
from xdsl.dialects.builtin import (
    AnyFloat,
    DenseIntOrFPElementsAttr,
    Float32Type,
    Float64Type,
    IntegerType,
    MemRefType,
    ModuleOp,
    NoneAttr,
    ShapedType,
    StridedLayoutAttr,
    SymbolRefAttr,
    UnrealizedConversionCastOp,
)
from xdsl.interpreters.ptr import TypedPtr
from xdsl.ir import Attribute, Operation, Region, SSAValue
from xdsl.passes import ModulePass
from xdsl.pattern_rewriter import (
    GreedyRewritePatternApplier,
    PatternRewriter,
    PatternRewriteWalker,
    RewritePattern,
    op_type_rewrite_pattern,
)
from xdsl.traits import SymbolTable
from xdsl.utils.exceptions import DiagnosticException


def bitwidth_of_type(type_attribute: Attribute) -> int:
    """
    Returns the width of an element type in bits, or raises DiagnosticException for unknown inputs.
    """
    if isinstance(type_attribute, AnyFloat):
        return type_attribute.get_bitwidth
    elif isinstance(type_attribute, IntegerType):
        return type_attribute.width.data
    else:
        raise NotImplementedError(
            f"Unsupported memref element type for riscv lowering: {type_attribute}"
        )


def element_size_for_type(type_attribute: Attribute) -> int:
    """
    Returns the width of an element type in bytes, or raises DiagnosticException for
    unknown inputs, or sizes not divisible by 8.
    """
    bitwidth = bitwidth_of_type(type_attribute)
    if bitwidth % 8:
        raise DiagnosticException(
            f"Cannot determine size for element type {type_attribute}"
            f" with bitwidth {bitwidth}"
        )
    bytes_per_element = bitwidth // 8
    return bytes_per_element


class ConvertMemrefAllocOp(RewritePattern):

    @op_type_rewrite_pattern
    def match_and_rewrite(self, op: memref.Alloc, rewriter: PatternRewriter) -> None:
        assert isinstance(op_memref_type := op.memref.type, memref.MemRefType)
        op_memref_type = cast(memref.MemRefType[Any], op_memref_type)
        width_in_bytes = bitwidth_of_type(op_memref_type.element_type) // 8
        size = prod(op_memref_type.get_shape()) * width_in_bytes
        rewriter.replace_matched_op(
            (
                size_op := riscv.LiOp(size, comment="memref alloc size"),
                move_op := riscv.MVOp(size_op.rd, rd=riscv.Registers.A0),
                call := riscv_func.CallOp(
                    SymbolRefAttr("malloc"),
                    (move_op.rd,),
                    (riscv.Registers.A0,),
                ),
                move_op := riscv.MVOp(call.ress[0], rd=riscv.Registers.UNALLOCATED_INT),
                UnrealizedConversionCastOp.get((move_op.rd,), (op.memref.type,)),
            )
        )


class ConvertMemrefDeallocOp(RewritePattern):
    @op_type_rewrite_pattern
    def match_and_rewrite(self, op: memref.Dealloc, rewriter: PatternRewriter) -> None:
        rewriter.replace_matched_op(
            (
                ptr := UnrealizedConversionCastOp.get(
                    (op.memref,), (riscv.Registers.UNALLOCATED_INT,)
                ),
                move_op := riscv.MVOp(ptr.results[0], rd=riscv.Registers.A0),
                riscv_func.CallOp(
                    SymbolRefAttr("free"),
                    (move_op.rd,),
                    (),
                ),
            )
        )


def get_strided_pointer(
    src_ptr: SSAValue,
    indices: Iterable[SSAValue],
    memref_type: MemRefType[Any],
) -> tuple[list[Operation], SSAValue]:
    """
    Given a buffer pointer 'src_ptr' which was originally of type 'memref_type', returns
    a new pointer to the element being accessed by the 'indices'.
    """

    bytes_per_element = element_size_for_type(memref_type.element_type)

    match memref_type.layout:
        case NoneAttr():
            strides = ShapedType.strides_for_shape(memref_type.get_shape())
        case StridedLayoutAttr():
            strides = memref_type.layout.get_strides()
        case _:
            raise DiagnosticException(f"Unsupported layout type {memref_type.layout}")

    ops: list[Operation] = []

    head: SSAValue | None = None

    for index, stride in zip(indices, strides, strict=True):
        # Calculate the offset that needs to be added through the index of the current
        # dimension.
        increment = index
        match stride:
            case None:
                raise DiagnosticException(
                    f"MemRef {memref_type} with dynamic stride is not yet implemented"
                )
            case 1:
                # Stride 1 is a noop making the index equal to the offset.
                pass
            case _:
                # Otherwise, multiply the stride (which by definition is the number of
                # elements required to be skipped when incrementing that dimension).
                ops.extend(
                    (
                        stride_op := riscv.LiOp(stride),
                        offset_op := riscv.MulOp(
                            increment,
                            stride_op.rd,
                            rd=riscv.IntRegisterType.unallocated(),
                        ),
                    )
                )
                stride_op.rd.name_hint = "pointer_dim_stride"
                offset_op.rd.name_hint = "pointer_dim_offset"
                increment = offset_op.rd

        if head is None:
            # First iteration.
            head = increment
            continue

        # Otherwise sum up the products.
        ops.append(
            add_op := riscv.AddOp(
                head, increment, rd=riscv.IntRegisterType.unallocated()
            )
        )
        add_op.rd.name_hint = "pointer_offset"
        head = add_op.rd

    if head is None:
        return ops, src_ptr

    ops.extend(
        [
            bytes_per_element_op := riscv.LiOp(bytes_per_element),
            offset_bytes := riscv.MulOp(
                head,
                bytes_per_element_op.rd,
                rd=riscv.IntRegisterType.unallocated(),
                comment="multiply by element size",
            ),
            ptr := riscv.AddOp(
                src_ptr, offset_bytes, rd=riscv.IntRegisterType.unallocated()
            ),
        ]
    )

    bytes_per_element_op.rd.name_hint = "bytes_per_element"
    offset_bytes.rd.name_hint = "scaled_pointer_offset"
    ptr.rd.name_hint = "offset_pointer"

    return ops, ptr.rd


class ConvertMemrefStoreOp(RewritePattern):
    @op_type_rewrite_pattern
    def match_and_rewrite(self, op: memref.Store, rewriter: PatternRewriter):
        assert isinstance(op_memref_type := op.memref.type, memref.MemRefType)
        memref_type = cast(memref.MemRefType[Any], op_memref_type)

        value, mem, *indices = cast_operands_to_regs(rewriter)

        shape = memref_type.get_shape()
        ops, ptr = get_strided_pointer(mem, indices, memref_type)

        rewriter.insert_op_before_matched_op(ops)
        match value.type:
            case riscv.IntRegisterType():
                new_op = riscv.SwOp(
                    ptr, value, 0, comment=f"store int value to memref of shape {shape}"
                )
            case riscv.FloatRegisterType():
                float_type = cast(AnyFloat, memref_type.element_type)
                match float_type:
                    case Float32Type():
                        new_op = riscv.FSwOp(
                            ptr,
                            value,
                            0,
                            comment=f"store float value to memref of shape {shape}",
                        )
                    case Float64Type():
                        new_op = riscv.FSdOp(
                            ptr,
                            value,
                            0,
                            comment=f"store double value to memref of shape {shape}",
                        )
                    case _:
                        assert False, f"Unexpected floating point type {float_type}"

            case _:
                assert False, f"Unexpected register type {value.type}"

        rewriter.replace_matched_op(new_op)


class ConvertMemrefLoadOp(RewritePattern):
    @op_type_rewrite_pattern
    def match_and_rewrite(self, op: memref.Load, rewriter: PatternRewriter):
        assert isinstance(
            op_memref_type := op.memref.type, memref.MemRefType
        ), f"{op.memref.type}"
        memref_type = cast(memref.MemRefType[Any], op_memref_type)

        mem, *indices = cast_operands_to_regs(rewriter)

        shape = memref_type.get_shape()
        ops, ptr = get_strided_pointer(mem, indices, memref_type)
        rewriter.insert_op_before_matched_op(ops)

        result_register_type = register_type_for_type(op.res.type)

        match result_register_type:
            case riscv.IntRegisterType:
                lw_op = riscv.LwOp(
                    ptr, 0, comment=f"load word from memref of shape {shape}"
                )
            case riscv.FloatRegisterType:
                float_type = cast(AnyFloat, memref_type.element_type)
                match float_type:
                    case Float32Type():
                        lw_op = riscv.FLwOp(
                            ptr, 0, comment=f"load float from memref of shape {shape}"
                        )
                    case Float64Type():
                        lw_op = riscv.FLdOp(
                            ptr, 0, comment=f"load double from memref of shape {shape}"
                        )
                    case _:
                        assert False, f"Unexpected floating point type {float_type}"

            case _:
                assert False, f"Unexpected register type {result_register_type}"

        rewriter.replace_matched_op(
            [
                lw := lw_op,
                UnrealizedConversionCastOp.get(lw.results, (op.res.type,)),
            ],
        )


class ConvertMemrefGlobalOp(RewritePattern):
    @op_type_rewrite_pattern
    def match_and_rewrite(self, op: memref.Global, rewriter: PatternRewriter):
        initial_value = op.initial_value

        if not isinstance(initial_value, DenseIntOrFPElementsAttr):
            raise DiagnosticException(
                f"Unsupported memref.global initial value: {initial_value}"
            )

        memref_type = cast(memref.MemRefType[Any], op.type)
        element_type = memref_type.element_type

        # Only handle a small subset of elements
        # Might be useful as a helper for other passes in the future
        match element_type:
            case IntegerType():
                bitwidth = element_type.width.data
                if bitwidth != 32:
                    raise DiagnosticException(
                        f"Unsupported memref element type for riscv lowering: {element_type}"
                    )
                ints = [d.value.data for d in initial_value.data]
                for i in ints:
                    assert isinstance(i, int)
                ints = cast(list[int], ints)
                ptr = TypedPtr.new_int32(ints).raw
            case Float32Type():
                floats = [d.value.data for d in initial_value.data]
                ptr = TypedPtr.new_float32(floats).raw
            case Float64Type():
                floats = [d.value.data for d in initial_value.data]
                ptr = TypedPtr.new_float64(floats).raw
            case _:
                raise DiagnosticException(
                    f"Unsupported memref element type for riscv lowering: {element_type}"
                )

        text = ",".join(hex(i) for i in ptr.int32.get_list(42))

        section = riscv.AssemblySectionOp(".data")
        with ImplicitBuilder(section.data):
            riscv.LabelOp(op.sym_name.data)
            riscv.DirectiveOp(".word", text)

        rewriter.replace_matched_op(section)


class ConvertMemrefGetGlobalOp(RewritePattern):
    @op_type_rewrite_pattern
    def match_and_rewrite(self, op: memref.GetGlobal, rewriter: PatternRewriter):
        rewriter.replace_matched_op(
            [
                ptr := riscv.LiOp(op.name_.string_value()),
                UnrealizedConversionCastOp.get((ptr,), (op.memref.type,)),
            ]
        )


class ConvertMemrefSubviewOp(RewritePattern):

    @op_type_rewrite_pattern
    def match_and_rewrite(self, op: memref.Subview, rewriter: PatternRewriter):
        # Assumes that the operation is valid, meaning that the subview is indeed a
        # subview, and that if the offset is stated in the layout attribute, then it's
        # correct.

        # From MLIR docs:
        # https://github.com/llvm/llvm-project/blob/4a9aef683df895934c26591404692d41a687b005/mlir/lib/Dialect/MemRef/Transforms/ExpandStridedMetadata.cpp#L173-L186
        # Replace `dst = subview(memref, sub_offset, sub_sizes, sub_strides))`
        # With
        #
        # \verbatim
        # source_buffer, source_offset, source_sizes, source_strides =
        #     extract_strided_metadata(memref)
        # offset = source_offset + sum(sub_offset#i * source_strides#i)
        # sizes = sub_sizes
        # strides#i = base_strides#i * sub_sizes#i
        # dst = reinterpret_cast baseBuffer, offset, sizes, strides
        # \endverbatim

        # This lowering does not preserve offset, sizes, and strides at runtime, instead
        # representing the memref as the base + offset directly, and relying on users of
        # the memref to use the information in the type to scale accesses.

        source = op.source
        result = op.result
        source_type = source.type
        assert isinstance(source_type, MemRefType)
        source_type = cast(MemRefType[Attribute], source_type)
        result_type = cast(MemRefType[Attribute], result.type)

        result_layout_attr = result_type.layout
        if isinstance(result_layout_attr, NoneAttr):
            # When a subview has no layout attr, the result is a perfect subview at offset
            # 0.
            rewriter.replace_matched_op(
                UnrealizedConversionCastOp.get((source,), (result_type,))
            )
            return

        if not isinstance(result_layout_attr, StridedLayoutAttr):
            raise DiagnosticException("Only strided layout attrs implemented")

        offset = result_layout_attr.get_offset()

        factor = element_size_for_type(result_type.element_type)

        if offset == 0:
            rewriter.replace_matched_op(
                UnrealizedConversionCastOp.get((source,), (result_type,))
            )
            return

        src = UnrealizedConversionCastOp.get(
            (source,), (riscv.IntRegisterType.unallocated(),)
        )
        src_rd = src.results[0]

        if offset is None:
            indices: list[SSAValue] = []
            index_ops: list[Operation] = []

            dynamic_offset_index = 0
            for static_offset_attr in op.static_offsets.data:
                static_offset = static_offset_attr.data
                assert isinstance(static_offset, int)
                if static_offset == memref.Subview.DYNAMIC_INDEX:
                    index_ops.append(
                        cast_index_op := UnrealizedConversionCastOp.get(
                            (op.offsets[dynamic_offset_index],),
                            (riscv.IntRegisterType.unallocated(),),
                        )
                    )
                    index_val = cast_index_op.results[0]
                    dynamic_offset_index += 1
                else:
                    # No need to insert arithmetic ops that will be multiplied by zero
                    index_ops.append(offset_op := riscv.LiOp(static_offset))
                    index_val = offset_op.rd
                index_val.name_hint = "subview_dim_index"
                indices.append(index_val)
            offset_ops, offset_rd = get_strided_pointer(src_rd, indices, source_type)
        else:
            factor_op = riscv.AddiOp(
                src_rd,
                offset * factor,
                comment="subview offset",
            )
            index_ops = []
            offset_ops = (factor_op,)
            offset_rd = factor_op.rd

        rewriter.replace_matched_op(
            (
                src,
                *index_ops,
                *offset_ops,
                UnrealizedConversionCastOp.get((offset_rd,), (result_type,)),
            )
        )


class ConvertMemrefToRiscvPass(ModulePass):
    name = "convert-memref-to-riscv"

    def apply(self, ctx: MLContext, op: ModuleOp) -> None:
        contains_malloc = PatternRewriteWalker(ConvertMemrefAllocOp()).rewrite_module(
            op
        )
        contains_dealloc = PatternRewriteWalker(
            ConvertMemrefDeallocOp()
        ).rewrite_module(op)
        PatternRewriteWalker(
            GreedyRewritePatternApplier(
                [
                    ConvertMemrefDeallocOp(),
                    ConvertMemrefStoreOp(),
                    ConvertMemrefLoadOp(),
                    ConvertMemrefGlobalOp(),
                    ConvertMemrefGetGlobalOp(),
                    ConvertMemrefSubviewOp(),
                ]
            )
        ).rewrite_module(op)
        if contains_malloc:
            func_op = riscv_func.FuncOp(
                "malloc",
                Region(),
                ((riscv.Registers.A0,), (riscv.Registers.A0,)),
                visibility="private",
            )
            SymbolTable.insert_or_update(op, func_op)
        if contains_dealloc:
            func_op = riscv_func.FuncOp(
                "free",
                Region(),
                ((riscv.Registers.A0,), ()),
                visibility="private",
            )
            SymbolTable.insert_or_update(op, func_op)
