"""Emit code and parser tables in either Python or Rust. """

import re
import unicodedata

from .runtime import (ERROR, ErrorToken)
from .ordered import OrderedSet

from .grammar import (InitNt, CallMethod, Some, is_concrete_element, Nt,
                      Optional)

from . import types


def write_python_parser(out, parser_states):
    grammar = parser_states.grammar
    states = parser_states.states
    prods = parser_states.prods
    init_state_map = parser_states.init_state_map

    out.write("from jsparagus import runtime\n")
    if any(isinstance(key, Nt) for key in grammar.nonterminals):
        out.write("from jsparagus.runtime import Nt, ErrorToken\n")
    out.write("\n")

    out.write("actions = [\n")
    for i, state in enumerate(states):
        out.write("    # {}. {}\n".format(i, state.traceback() or "<empty>"))
        # for item in state._lr_items:
        #     out.write("    #       {}\n".format(grammar.lr_item_to_str(prods, item)))
        out.write("    " + repr(state.action_row) + ",\n")
        out.write("\n")
    out.write("]\n\n")
    out.write("ctns = [\n")
    for state in states:
        row = {
            nt.pretty(): state_id
            for nt, state_id in state.ctn_row.items()
        }
        out.write("    " + repr(row) + ",\n")
    out.write("]\n\n")
    out.write("error_codes = [\n")
    SLICE_LEN = 16
    for i in range(0, len(states), SLICE_LEN):
        slice = states[i:i + SLICE_LEN]
        out.write("    {}\n".format(
            " ".join(repr(e.error_code) + "," for e in slice)))
    out.write("]\n\n")

    def compile_reduce_expr(expr):
        """Compile a reduce expression to Python"""
        if isinstance(expr, CallMethod):
            method_name = expr.method.replace(" ", "_P")
            return "builder.{}({})".format(method_name, ', '.join(map(compile_reduce_expr, expr.args)))
        elif isinstance(expr, Some):
            return compile_reduce_expr(expr.inner)
        elif expr is None:
            return "None"
        else:
            # can't be 'accept' because we filter out InitNt productions
            assert isinstance(expr, int)
            return "x{}".format(expr)

    out.write("reductions = [\n")
    for prod_index, prod in enumerate(prods):
        if isinstance(prod.nt.name, InitNt):
            continue
        nparams = sum(1 for e in prod.rhs if is_concrete_element(e))
        names = ["x" + str(i) for i in range(nparams)]
        fn = ("lambda builder, "
              + ", ".join(names)
              + ": " + compile_reduce_expr(prod.reducer))
        out.write("    # {}. {}\n".format(
            prod_index,
            grammar.production_to_str(prod.nt, prod.rhs, prod.reducer)))
        out.write("    ({!r}, {!r}, {}),\n".format(prod.nt.pretty(), len(names), fn))
    out.write("]\n\n\n")  # two blank lines before class.

    out.write("class DefaultBuilder:\n")
    for tag, method_type in grammar.methods.items():
        method_name = tag.replace(' ', '_P')
        args = ", ".join("x{}".format(i)
                         for i in range(len(method_type.argument_types)))
        out.write("    def {}(self, {}): return ({!r}, {})\n"
                  .format(method_name, args, tag, args))
    out.write("\n\n")

    out.write("goal_nt_to_init_state = {\n")
    for init_nt, index in init_state_map.items():
        out.write("    {!r}: {!r},\n".format(init_nt.name, index))
    out.write("}\n\n")

    if len(init_state_map) == 1:
        init_nt = next(iter(init_state_map.keys()))
        default_goal = '=' + repr(init_nt.name)
    else:
        default_goal = ''
    out.write("class Parser(runtime.Parser):\n")
    out.write("    def __init__(self, goal{}, builder=None):\n".format(default_goal))
    out.write("        if builder is None:\n")
    out.write("            builder = DefaultBuilder()\n")
    out.write("        super().__init__(actions, ctns, reductions, error_codes,\n")
    out.write("                         goal_nt_to_init_state[goal], builder)\n")
    out.write("\n")


TERMINAL_NAMES = {
    "=>": "Arrow",
}

# List of output method names that are fallible and must therefore be called
# with a trailing `?`. A bad hack which we need to fix by having more type
# information about output methods.
FALLIBLE_METHOD_NAMES = {
    'assignment_expression',
    'compound_assignment_expression',
    'expression_to_parameter_list',
    'for_assignment_target',
    'for_await_of_statement',
    'post_decrement_expr',
    'post_increment_expr',
    'pre_decrement_expr',
    'pre_increment_expr',
}

class RustParserWriter:
    def __init__(self, out, parser_states):
        self.out = out
        self.grammar = parser_states.grammar
        self.prods = parser_states.prods
        self.states = parser_states.states
        self.init_state_map = parser_states.init_state_map
        self.terminals = list(OrderedSet(
            t for state in self.states for t in state.action_row))
        self.nonterminals = list(OrderedSet(
            nt for state in self.states for nt in state.ctn_row))

    def emit(self):
        self.header()
        self.terminal_id()
        self.token()
        self.actions()
        self.error_codes()
        self.check_camel_case()
        # self.nt_node()
        # self.nt_node_impl()
        self.nonterminal_id()
        self.goto()
        self.reduce()
        self.reduce_simulator()
        self.entry()

    def write(self, indentation, string, *format_args):
        if len(format_args) == 0:
            formatted = string
        else:
            formatted = string.format(*format_args)
        self.out.write("    " * indentation + formatted + "\n")

    def header(self):
        self.write(0, "// WARNING: This file is autogenerated.")
        self.write(0, "")
        self.write(0, "use ast::{arena::{Box, Vec}, types::*};")
        self.write(0, "use crate::ast_builder::AstBuilder;")
        self.write(0, "use crate::stack_value_generated::{StackValue, TryIntoStack};")
        self.write(0, "use crate::error::Result;")
        self.write(0, "")
        self.write(0, "const ERROR: i64 = {};", hex(ERROR))
        self.write(0, "")

    def terminal_name(self, value):
        if value is None:
            return "End"
        elif value is ErrorToken:
            return "ErrorToken"
        elif value in TERMINAL_NAMES:
            return TERMINAL_NAMES[value]
        elif value.isalpha():
            if value.islower():
                return value.capitalize()
            else:
                return value
        else:
            raw_name = " ".join((unicodedata.name(c) for c in value))
            snake_case = raw_name.replace("-", " ").replace(" ", "_").lower()
            camel_case = self.to_camel_case(snake_case)
            return camel_case

    def terminal_name_camel(self, value):
        return self.to_camel_case(self.terminal_name(value))

    def terminal_id(self):
        self.write(0, "#[derive(Copy, Clone, Debug, PartialEq)]")
        self.write(0, "pub enum TerminalId {")
        for i, t in enumerate(self.terminals):
            name = self.terminal_name(t)
            self.write(1, "{} = {}, // {}", name, i, repr(t))
        self.write(0, "}")
        self.write(0, "")

    def token(self):
        self.write(0, "#[derive(Clone, Debug, PartialEq)]")
        self.write(0, "pub struct Token<'a> {")
        self.write(1, "pub terminal_id: TerminalId,")
        self.write(1, "pub saw_newline: bool,")
        self.write(1, "pub value: Option<&'a str>,")
        self.write(0, "}")
        self.write(0, "")

        self.write(0, "impl Token<'_> {")
        self.write(1, "pub fn basic_token(terminal_id: TerminalId) -> Self {")
        self.write(2, "Self {")
        self.write(3, "terminal_id,")
        self.write(3, "saw_newline: false,")
        self.write(3, "value: None,")
        self.write(2, "}")
        self.write(1, "}")
        self.write(0, "")

        self.write(1, "pub fn into_static(self) -> Token<'static> {")
        self.write(2, "Token {")
        self.write(3, "terminal_id: self.terminal_id,")
        self.write(3, "saw_newline: self.saw_newline,")
        self.write(3, "value: None,")  # drop the value, which has limited lifetime
        self.write(2, "}")
        self.write(1, "}")
        self.write(0, "}")
        self.write(0, "")

    def actions(self):
        self.write(0, "#[rustfmt::skip]")
        self.write(0, "static ACTIONS: [i64; {}] = [",
                   len(self.states) * len(self.terminals))
        for i, state in enumerate(self.states):
            self.write(1, "// {}. {}", i, state.traceback() or "<empty>")
            self.write(1, "{}",
                       ' '.join("{},".format(state.action_row.get(t, "ERROR")) for t in self.terminals))
            if i < len(self.states) - 1:
                self.write(0, "")
        self.write(0, "];")
        self.write(0, "")

    def error_codes(self):
        self.write(0, "#[derive(Clone, Debug, PartialEq)]")
        self.write(0, "pub enum ErrorCode {")
        for error_code in OrderedSet(s.error_code for s in self.states):
            if error_code is not None:
                self.write(1, "{},", self.to_camel_case(error_code))
        self.write(0, "}")
        self.write(0, "")

        self.write(0, "static STATE_TO_ERROR_CODE: [Option<ErrorCode>; {}] = [",
                   len(self.states))
        for i, state in enumerate(self.states):
            self.write(1, "// {}. {}", i, state.traceback() or "<empty>")
            if state.error_code is None:
                self.write(1, "None,")
            else:
                self.write(1, "Some(ErrorCode::{}),",
                           self.to_camel_case(state.error_code))
        self.write(0, "];")
        self.write(0, "")

    def nonterminal_to_snake(self, ident):
        if isinstance(ident, Nt):
            base_name = self.to_snek_case(ident.name)
            args = ''.join((("_" + self.to_snek_case(name))
                            for name, value in ident.args if value))
            return base_name + args
        else:
            assert isinstance(ident, str)
            return self.to_snek_case(ident)

    def nonterminal_to_camel(self, nt):
        return self.to_camel_case(self.nonterminal_to_snake(nt))

    def to_camel_case(self, ident):
        if '_' in ident:
            return ''.join(word.capitalize() for word in ident.split('_'))
        elif ident.islower():
            return ident.capitalize()
        else:
            return ident

    def check_camel_case(self):
        seen = {}
        for nt in self.nonterminals:
            cc = self.nonterminal_to_camel(nt)
            if cc in seen:
                raise ValueError("{} and {} have the same camel-case spelling ({})".format(
                    seen[cc], nt, cc))
            seen[cc] = nt

    def to_snek_case(self, ident):
        # https://stackoverflow.com/questions/1175208
        s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', ident)
        return re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()

    def method_name_to_rust(self, name):
        """Convert jsparagus's internal method name to idiomatic Rust."""
        nt_name, space, number = name.partition(' ')
        name = self.nonterminal_to_snake(nt_name)
        if space:
            name += "_p" + str(number)
        return name

    def get_associated_type_names(self):
        names = OrderedSet()

        def visit_type(ty):
            for arg in ty.args:
                visit_type(arg)
            if len(ty.args) == 0:
                names.add(ty.name)

        for ty in self.grammar.nt_types:
            visit_type(ty)
        for method in self.grammar.methods.values():
            visit_type(method.return_type)
        return names

    def type_to_rust(self, ty, namespace, boxed=False):
        """
        Convert a jsparagus type (see types.py) to Rust.

        Pass boxed=True if the type needs to be boxed.
        """
        if ty == types.UnitType:
            assert not boxed
            rty = '()'
        elif ty == types.TokenType:
            rty = "Token<'alloc>"
        elif ty.name == 'Option' and len(ty.args) == 1:
            # We auto-translate `Box<Option<T>>` to `Option<Box<T>>` since
            # that's basically the same thing but more efficient.
            [arg] = ty.args
            return 'Option<{}>'.format(self.type_to_rust(arg, namespace, boxed))
        elif ty.name == 'Vec' and len(ty.args) == 1:
            [arg] = ty.args
            rty = "Vec<'alloc, {}>".format(self.type_to_rust(arg, namespace, boxed=False))
        else:
            if namespace == "":
                rty = ty.name
            else:
                rty = namespace + '::' + ty.name
            if ty.args:
                rty += '<{}>'.format(', '.join(self.type_to_rust(arg, namespace, boxed)
                                               for arg in ty.args))
        if boxed:
            return "Box<'alloc, {}>".format(rty)
        else:
            return rty

    def handler_trait(self):
        # NOTE: unused, code kept if we need it later
        self.write(0, "pub trait Handler {")

        for name in self.get_associated_type_names():
            self.write(1, "type {};", name)

        for tag, method in self.grammar.methods.items():
            method_name = self.method_name_to_rust(tag)
            arg_types = [
                self.type_to_rust(ty, "Self")
                for ty in method.argument_types
                if ty != types.UnitType
            ]
            if method.return_type == types.UnitType:
                return_type_tag = ''
            else:
                return_type_tag = ' -> ' + \
                    self.type_to_rust(method.return_type, "Self")

            args = ", ".join(("a{}: {}".format(i, t)
                              for i, t in enumerate(arg_types)))
            self.write(1, "fn {}(&self, {}){};",
                       method_name, args, return_type_tag)
        self.write(0, "}")
        self.write(0, "")

    def nt_node(self):
        self.write(0, "pub mod concrete {")
        for name in self.get_associated_type_names():
            self.write(0, "#[derive(Debug, PartialEq)]")
            self.write(0, "pub enum {} {{", name)
            for tag, method in self.grammar.methods.items():
                # TODO: Make this check better
                if method.return_type.name != name:
                    continue
                method_name = self.to_camel_case(self.method_name_to_rust(tag))
                arg_types = [
                    self.type_to_rust(ty, "", boxed=True)
                    for ty in method.argument_types
                    if ty != types.UnitType
                ]
                self.write(1, "{}({}),", method_name, ", ".join(arg_types))
            self.write(0, "}")
            self.write(0, "")
        self.write(0, "}")
        self.write(0, "")

    def nt_node_impl(self):
        method_to_prod = {}
        for i, prod in enumerate(self.prods):
            if prod.nt in self.nonterminals:
                def find_first_method(expr):
                    if isinstance(expr, CallMethod):
                        return self.method_name_to_rust(expr.method)
                    elif isinstance(expr, Some):
                        return find_first_method
                    elif expr is None:
                        return None
                    else:
                        assert isinstance(expr, int)
                        return None

                method_name = find_first_method(prod.reducer)
                if method_name is not None:
                    method_to_prod[method_name] = self.grammar.production_to_str(
                        prod.nt, prod.rhs, prod.reducer)

        # for method in self.grammar.methods.items():
        # if prod.nt in self.nonterminals:
        #    self.write(2, "{} => {{", i)
        #    self.write(3, "// {}",

        self.write(0, "pub struct DefaultHandler {}")
        self.write(0, "")
        self.write(0, "impl DefaultHandler {")

        for tag, method in self.grammar.methods.items():
            method_name = self.method_name_to_rust(tag)
            if method_name in method_to_prod:
                prod_name = method_to_prod[method_name]
            else:
                prod_name = "<unknown production>"
            method_name_camel = self.to_camel_case(method_name)
            arg_types = [
                self.type_to_rust(ty, "concrete", boxed=True)
                for ty in method.argument_types
                if ty != types.UnitType
            ]
            if method.return_type == types.UnitType:
                return_type_tag = ''
            else:
                return_type_tag = ' -> ' + \
                    self.type_to_rust(method.return_type, "concrete", boxed=True)

            args = "".join(", a{}: {}".format(i, t)
                           for i, t in enumerate(arg_types))
            params = ", ".join("a{}".format(i)
                               for i, t in enumerate(arg_types))

            self.write(1, "// {}", prod_name)
            self.write(1, "fn {}(&self{}){} {{",
                       method_name, args, return_type_tag)
            self.write(2, "Box::new(concrete::{}::{}({}))",
                       method.return_type.name, method_name_camel, params)
            self.write(1, "}")
        self.write(0, "}")
        self.write(0, "")

    def nonterminal_id(self):
        self.write(0, "#[derive(Clone, Copy, Debug, PartialEq)]")
        self.write(0, "pub enum NonterminalId {")
        for i, nt in enumerate(self.nonterminals):
            self.write(1, "{} = {},", self.nonterminal_to_camel(nt), i)
        self.write(0, "}")
        self.write(0, "")

    def goto(self):
        self.write(0, "#[rustfmt::skip]")
        self.write(0, "static GOTO: [u16; {}] = [",
                   len(self.states) * len(self.nonterminals))
        for state in self.states:
            row = state.ctn_row
            self.write(1, "{}", ' '.join("{},".format(row.get(nt, 0))
                                         for nt in self.nonterminals))
        self.write(0, "];")
        self.write(0, "")

    def element_type(self, e):
        # Mostly duplicated from types.py. :(
        g = self.grammar
        if isinstance(e, str):
            if e in g.nonterminals:
                assert nt_types[e] is not None
                return nt_types[e]
            elif e in g.variable_terminals:
                return types.TokenType
            else:
                # constant terminal
                return types.UnitType
        elif isinstance(e, Optional):
            return Type('Option', [element_type(e.inner)])
        elif isinstance(e, Nt):
            # Cope with the awkward fact that g.nonterminals keys may be either
            # strings or Nt objects.
            nt_key = e if e in g.nonterminals else e.name
            assert g.nonterminals[nt_key].type is not None
            return g.nonterminals[nt_key].type
        else:
            assert False, "unexpected element type: {!r}".format(e)

    def reduce(self):
        # Note use of std::vec::Vec below: we have imported `arena::Vec` in this module,
        # since every other data structure mentioned in this file lives in the arena.
        self.write(0, "pub fn reduce<'alloc>(")
        self.write(1, "handler: &AstBuilder<'alloc>,")
        self.write(1, "prod: usize,")
        self.write(1, "stack: &mut std::vec::Vec<StackValue<'alloc>>,")
        self.write(0, ") -> Result<'alloc, NonterminalId> {")
        self.write(1, "match prod {")
        for i, prod in enumerate(self.prods):
            # If prod.nt is not in nonterminals, that means it's a goal
            # nonterminal, only accepted, never reduced.
            if prod.nt in self.nonterminals:
                self.write(2, "{} => {{", i)
                self.write(3, "// {}",
                           self.grammar.production_to_str(prod.nt, prod.rhs, prod.reducer))

                # At run time, the top of the stack will be one value per
                # concrete symbol in the RHS of the production we're reducing.
                # We are about to emit code to pop these values from the stack,
                # one at a time. They come off the stack in reverse order.
                elements = [e for e in prod.rhs if is_concrete_element(e)]

                # We can emit three different kinds of code here:
                #
                # 1.  Full compilation. Pop each value from the stack; if it's
                #     used, downcast it to its actual type and store it in a
                #     local variable (otherwise just drop it). Then, evaulate
                #     the reduce-expression. Push the result back onto the
                #     stack.
                #
                # 2.  `is_discarding_reduction`: A reduce expression that is
                #     just an integer is retaining one stack value and dropping
                #     the rest. We skip the downcast in this case.
                #
                # 3.  `is_trivial_reduction`: A production has only one
                #     concrete symbol in it, and the reducer is just `0`.
                #     We don't have to do anything at all here.
                is_trivial_reduction = len(elements) == 1 and prod.reducer == 0
                is_discarding_reduction = isinstance(prod.reducer, int)

                # While compiling, figure out which elements are used.
                variable_used = [False] * len(elements)

                def compile_reduce_expr(expr):
                    """Compile a reduce expression to Rust"""
                    if isinstance(expr, CallMethod):
                        method_type = self.grammar.methods[expr.method]
                        method_name = self.method_name_to_rust(expr.method)
                        assert len(method_type.argument_types) == len(expr.args)
                        args = ', '.join(
                            compile_reduce_expr(arg)
                            for ty, arg in zip(method_type.argument_types,
                                               expr.args)
                            if ty != types.UnitType)
                        call = "handler.{}({})".format(method_name, args)

                        # Extremely bad hack. In Rust, since type inference is
                        # currently so poor, we don't have enough information
                        # to know if this method can fail or not, and Rust
                        # requires us to know that.
                        if method_name in FALLIBLE_METHOD_NAMES:
                            call += "?"
                        return call
                    elif isinstance(expr, Some):
                        return "Some({})".format(compile_reduce_expr(expr.inner))
                    elif expr is None:
                        return "None"
                    else:
                        # can't be 'accept' because we filter out InitNt productions
                        assert isinstance(expr, int)
                        variable_used[expr] = True
                        return "x{}".format(expr)

                compiled_expr = compile_reduce_expr(prod.reducer)

                if not is_trivial_reduction:
                    for index, e in reversed(list(enumerate(elements))):
                        if variable_used[index]:
                            ty = self.element_type(e)
                            rust_ty = self.type_to_rust(ty, "", boxed=True)
                            if is_discarding_reduction:
                                self.write(3, "let x{} = stack.pop().unwrap();", index)
                            else:
                                self.write(3, "let x{}: {} = stack.pop().unwrap().to_ast();", index, rust_ty)
                        else:
                            self.write(3, "stack.pop();", index)

                    if is_discarding_reduction:
                        self.write(3, "stack.push({});", compiled_expr)
                    else:
                        self.write(3, "stack.push(TryIntoStack::try_into_stack({})?);", compiled_expr)

                self.write(3, "Ok(NonterminalId::{})",
                           self.nonterminal_to_camel(prod.nt))
                self.write(2, "}")
        self.write(2, '_ => panic!("no such production: {}", prod),')
        self.write(1, "}")
        self.write(0, "}")
        self.write(0, "")

    def reduce_simulator(self):
        prods = [prod for prod in self.prods if prod.nt in self.nonterminals]
        self.write(0, "static REDUCE_SIMULATOR: [(usize, NonterminalId); {}] = [", len(prods))
        for prod in prods:
            elements = [e for e in prod.rhs if is_concrete_element(e)]
            self.write(1, "({}, NonterminalId::{}),", len(elements), self.nonterminal_to_camel(prod.nt))
        self.write(0, "];")
        self.write(0, "")

    def entry(self):
        self.write(0, "#[derive(Clone, Copy)]")
        self.write(0, "pub struct ParserTables<'a> {")
        self.write(1, "pub state_count: usize,")
        self.write(1, "pub action_table: &'a [i64],")
        self.write(1, "pub action_width: usize,")
        self.write(1, "pub error_codes: &'a [Option<ErrorCode>],")
        self.write(1, "pub reduce_simulator: &'a [(usize, NonterminalId)],")
        self.write(1, "pub goto_table: &'a [u16],")
        self.write(1, "pub goto_width: usize,")
        self.write(0, "}")
        self.write(0, "")

        self.write(0, "impl<'a> ParserTables<'a> {")
        self.write(1, "pub fn check(&self) {")
        self.write(2, "assert_eq!(")
        self.write(3, "self.action_table.len(),")
        self.write(3, "(self.state_count * self.action_width) as usize")
        self.write(2, ");")
        self.write(2, "assert_eq!(self.goto_table.len(), (self.state_count * self.goto_width) as usize);")
        self.write(1, "}")
        self.write(0, "}")
        self.write(0, "")

        self.write(0, "pub static TABLES: ParserTables<'static> = ParserTables {")
        self.write(1, "state_count: {},", len(self.states))
        self.write(1, "action_table: &ACTIONS,")
        self.write(1, "action_width: {},", len(self.terminals))
        self.write(1, "error_codes: &STATE_TO_ERROR_CODE,")
        self.write(1, "reduce_simulator: &REDUCE_SIMULATOR,")
        self.write(1, "goto_table: &GOTO,")
        self.write(1, "goto_width: {},".format(len(self.nonterminals)))
        self.write(0, "};")
        self.write(0, "")

        for init_nt, index in self.init_state_map.items():
            assert init_nt.args == ()
            self.write(0, "pub static START_STATE_{}: usize = {};",
                       self.to_snek_case(init_nt.name).upper(), index)
            self.write(0, "")


def write_rust_parser(out, parser_states):
    RustParserWriter(out, parser_states).emit()
