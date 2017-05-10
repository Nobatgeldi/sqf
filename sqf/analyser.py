from copy import deepcopy

from sqf.types import Statement, Code, Nothing, Variable, Array, String, Type, File, BaseType, Number, Object
from sqf.interpreter_types import InterpreterType, PrivateType, ForType
from sqf.keywords import Keyword, PREPROCESSORS
from sqf.expressions import UnaryExpression, BinaryExpression
from sqf.exceptions import SQFParserError, SQFWarning
from sqf.base_interpreter import BaseInterpreter
from sqf.database import EXPRESSIONS
from sqf.common_expressions import COMMON_EXPRESSIONS
from sqf.expressions_cache import values_to_expressions, build_database


# Replace all expressions in `database` by expressions from `COMMON_EXPRESSIONS` with the same signature
for exp in COMMON_EXPRESSIONS:
    if exp in EXPRESSIONS:
        EXPRESSIONS.remove(exp)
    EXPRESSIONS.append(exp)


EXPRESSIONS_MAP = build_database(EXPRESSIONS)


class Analyzer(BaseInterpreter):
    """
    The Analyzer. This is sesentially an interpreter that
    * runs SQF statements that accepts unknown types
    * Stores exceptions instead of rising them.
    * Runs code that is declared but not called.
    """
    def __init__(self, all_vars=None):
        super().__init__(all_vars)
        self.exceptions = []

        self.privates = []
        self.unevaluated_interpreter_tokens = []
        self._unexecuted_codes = {}
        self._executed_codes = {}
        self.defines = {}

    def exception(self, exception):
        self.exceptions.append(exception)

    def value(self, token, namespace_name=None):
        """
        Given a single token, recursively evaluates and returns its value
        """
        assert(isinstance(token, BaseType))
        if isinstance(token, Statement):
            result = self.value(self.execute_token(token))
        elif isinstance(token, Variable):
            scope = self.get_scope(token.name, namespace_name)
            if scope.level == 0 and not token.is_global:
                self.exception(SQFWarning(token.position, 'Local variable "%s" is not from this scope (not private)' % token))

            result = scope[token.name]
            result.position = token.position
        elif isinstance(token, Array) and not token.is_undefined:
            result = Array([self.value(self.execute_token(s)) for s in token.value])
            result.position = token.position
        else:
            null_expressions = values_to_expressions([token], EXPRESSIONS_MAP, EXPRESSIONS)
            if null_expressions:
                result = null_expressions[0].execute([token], self)
            else:
                result = token
            result.position = token.position

        if isinstance(result, Code) and str(result) not in self._unexecuted_codes:
            self._unexecuted_codes[str(result)] = result

        return result

    def execute_token(self, token):
        """
        Given a single token, recursively evaluate it without returning its value (only type)
        """
        # interpret the statement recursively
        if isinstance(token, Statement):
            result = self.execute_single(statement=token)
            # we do not want the position of the statement, but of the token, so we do not
            # store it here
        elif isinstance(token, Array) and token.value is not None:
            result = Array([self.execute_token(s) for s in token.value])
            result.position = token.position
        elif str(token) in self.defines:
            result = self.execute_token(self.defines[str(token)])
            result.position = token.position
        else:
            result = token
            result.position = token.position

        return result

    def execute_unexecuted_code(self, code):
        """
        Executes a code in a dedicated env and put consequence exceptions in self.
        """
        assert(str(code) in self._unexecuted_codes)

        analyser = Analyzer()
        analyser._namespaces = deepcopy(self._namespaces)
        analyser._current_namespace = analyser._namespaces['missionnamespace']

        file = File(code._tokens)
        file.position = code.position

        analyser.execute_code(file)

        self.exceptions.extend(analyser.exceptions)

    def execute_code(self, code, params=None, extra_scope=None):
        if str(code) in self._unexecuted_codes:
            del self._unexecuted_codes[str(code)]
        self._executed_codes[str(code)] = code

        outcome = super().execute_code(code, params, extra_scope)

        # collect `private` statements that have a variable but were not collected by the assignment operator
        if isinstance(code, File):
            for code in self._unexecuted_codes:
                self.execute_unexecuted_code(self._unexecuted_codes[code])

            for private in self.privates:
                self.exception(SQFWarning(private.position, 'private argument must be a string.'))

            for token in self.unevaluated_interpreter_tokens:
                self.exception(SQFWarning(token.position, 'helper type "%s" not evaluated' % token.__class__.__name__))

        return outcome

    def execute_single(self, statement):
        assert(isinstance(statement, Statement))

        outcome = Nothing()
        outcome.position = statement.position

        base_tokens = statement.base_tokens
        if not base_tokens:
            return outcome

        # operations that cannot evaluate the value of all base_tokens
        if base_tokens[0] == Keyword('#define'):
            if len(base_tokens) < 2:
                exception = SQFParserError(base_tokens[0].position, "#define must have at least one argument")
                self.exception(exception)
            elif len(base_tokens) == 2: # e.g. #define a
                value = Nothing()
                value.position = base_tokens[1].position
                self.defines[str(base_tokens[1])] = value
            elif len(base_tokens) == 3:  # e.g. #define a 2
                self.defines[str(base_tokens[1])] = base_tokens[2]
            else:  # e.g. #define a(_x) b(_x)
                define_statement = Statement(statement.base_tokens[3:])
                define_statement.position = base_tokens[3].position
                self.defines[str(base_tokens[1])] = define_statement
            return outcome
        elif base_tokens[0] == Keyword("#include"):
            if len(base_tokens) != 2 or type(base_tokens[1]) != String:
                exception = SQFParserError(base_tokens[0].position, "Wrong syntax for #include")
                self.exception(exception)
            return outcome
        elif isinstance(base_tokens[0], Keyword) and base_tokens[0].value in PREPROCESSORS:
            # remaining preprocessors are ignored
            return outcome
        elif len(base_tokens) == 2 and base_tokens[0] == Keyword('private'):
            # the rhs may be a variable, so we cannot get the value
            rhs = self.execute_token(base_tokens[1])
            if isinstance(rhs, String):
                self.add_privates([rhs])
            elif isinstance(rhs, Array):
                self.add_privates(self.value(rhs))
            elif isinstance(rhs, Variable):
                self.add_privates([String('"' + rhs.name + '"')])
                outcome = PrivateType(rhs)
                outcome.position = rhs.position
                self.privates.append(outcome)
            else:
                self.exception(SQFParserError(base_tokens[0].position, '`private` used incorrectly'))
            return outcome
        # assignment operator
        elif len(base_tokens) == 3 and base_tokens[1] == Keyword('='):
            lhs = self.execute_token(base_tokens[0])
            if isinstance(lhs, PrivateType):
                self.privates.remove(lhs)
                lhs = lhs.variable
            else:
                lhs = self.get_variable(base_tokens[0])

            rhs_v = self.value(base_tokens[2])
            rhs_t = type(rhs_v)

            if not isinstance(lhs, Variable):
                self.exception(SQFParserError(base_tokens[0].position, 'lhs of assignment operator must be a variable'))
            else:
                scope = self.get_scope(lhs.name)

                lhs_t = type(scope[lhs.name])
                if lhs_t != Nothing and lhs_t != rhs_t:
                    rhs_t = Nothing

                scope[lhs.name] = rhs_t()

                if scope.level == 0 and not lhs.is_global:
                    self.exception(
                        SQFWarning(lhs.position, 'Local variable "%s" assigned to an outer scope (not private)' % lhs.name))

                if not statement.ending:
                    outcome = rhs_v
            return outcome
        # A variable can only be evaluated if we need its value, so we will not call its value until the very end.
        elif len(base_tokens) == 1 and type(base_tokens[0]) in (Variable, Array):
            return self.execute_token(base_tokens[0])
        # heuristic for defines (that are thus syntactically correct):
        #   - global variable followed by a parenthesis statement
        #   - first token string is all upper
        #   - first token is a define
        elif len(base_tokens) == 2 and (
                            type(base_tokens[0]) == Variable and base_tokens[0].is_global or
                            str(base_tokens[0]).isupper() or
                            str(base_tokens[0]) in self.defines) and \
                type(base_tokens[1]) == Statement and base_tokens[1].parenthesis:
            return outcome

        # evaluate all the base_tokens, trying to obtain their values
        values = []
        tokens = []
        for token in base_tokens:
            t = self.execute_token(token)
            v = self.value(t)
            tokens.append(t)
            values.append(v)

        # try to find a match for any expression, both typed and un-typed
        case_found = None
        possible_expressions = values_to_expressions(values, EXPRESSIONS_MAP, EXPRESSIONS)
        for case in possible_expressions:
            if case.is_signature_match(values):  # match first occurrence
                case_found = case
                break

        if case_found:
            # if exact match, we run the expression.
            if case_found.is_match(values):
                outcome = case_found.execute(values, self)
            elif len(possible_expressions) == 1 and possible_expressions[0].return_type is not None:
                outcome = possible_expressions[0].return_type()

            extra_scope = None
            if case_found.keyword in (Keyword('select'), Keyword('apply'), Keyword('count')):
                extra_scope = {'_x': Nothing()}
            elif case_found.keyword == Keyword('foreach'):
                extra_scope = {'_foreachindex': Number(), '_x': Nothing()}
            elif case_found.keyword == Keyword('catch'):
                extra_scope = {'_exception': Object()}
            elif case_found.keyword == Keyword('do') and type(values[0]) == ForType:
               extra_scope = {'_i': Number()}
            for value, t_or_v in zip(values, case_found.types_or_values):
                # execute all pieces of code
                if t_or_v == Code and isinstance(value, Code):
                    self.execute_code(value, extra_scope=extra_scope)

                # remove evaluated interpreter tokens
                if isinstance(value, InterpreterType) and value in self.unevaluated_interpreter_tokens:
                    self.unevaluated_interpreter_tokens.remove(value)

            assert(isinstance(outcome, Type))
        elif len(values) == 1:
            if not isinstance(values[0], Type):
                self.exception(
                    SQFParserError(statement.position, '"%s" is syntactically incorrect (missing ;?)' % statement))
            outcome = values[0]
        elif isinstance(base_tokens[0], Variable) and base_tokens[0].is_global:
            # statements starting with a global are likely defined somewhere else
            # todo: catch globals with statements and without statements
            pass
        elif len(possible_expressions) > 0:
            if isinstance(possible_expressions[0], UnaryExpression):
                types_or_values = []
                for exp in possible_expressions:
                    types_or_values.append(exp.types_or_values[1].__name__)

                keyword_name = possible_expressions[0].types_or_values[0].value

                message = 'Unary operator "%s" only accepts argument of types [%s] (rhs is %s)' % \
                          (keyword_name, ','.join(types_or_values), values[1].__class__.__name__)
            elif isinstance(possible_expressions[0], BinaryExpression):
                types_or_values = []
                for exp in possible_expressions:
                    types_or_values.append('(%s,%s)' % (exp.types_or_values[0].__name__, exp.types_or_values[2].__name__))

                keyword_name = possible_expressions[0].types_or_values[1].value

                message = 'Binary operator "{0}" arguments must be [{1}]'.format(
                    keyword_name, ','.join(types_or_values))
                if values[0].__class__.__name__ not in [x[0] for x in types_or_values]:
                    message += ' (lhs is %s' % values[0].__class__.__name__
                if values[0].__class__.__name__ not in [x[1] for x in types_or_values]:
                    message += ', rhs is %s)' % values[2].__class__.__name__
                else:
                    message += ')'
            else:
                assert False

            self.exception(SQFParserError(tokens[0].position, message))
        else:
            self.exception(
                SQFParserError(tokens[0].position, 'statement is syntactically incorrect (missing ;?)'))

        if isinstance(outcome, InterpreterType):
            self.unevaluated_interpreter_tokens.append(outcome)

        assert(isinstance(outcome, BaseType))
        # the position of Private is different because it can be passed from analyser to analyser,
        # and we want to keep the position of the outermost analyser.
        if not isinstance(outcome, PrivateType):
            outcome.position = statement.position

        if statement.ending:
            outcome = Nothing()
            outcome.position = statement.position

        return outcome


def analyze(statement, analyzer=None):
    assert (isinstance(statement, Statement))
    if analyzer is None:
        analyzer = Analyzer()

    file = File(statement._tokens)

    file.position = (1, 1)

    arg = Nothing()
    arg.position = (1, 1)

    analyzer.execute_code(file, extra_scope={'_this': arg})

    return analyzer
