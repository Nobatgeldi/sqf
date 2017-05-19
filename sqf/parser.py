from collections import defaultdict
import re

import sqf.base_type
from sqf.base_tokenizer import tokenize

from sqf.exceptions import SQFParenthesisError, SQFParserError
from sqf.types import Statement, Code, Number, Boolean, Variable, Array, String, Keyword, Namespace, Preprocessor, ParserType
from sqf.keywords import KEYWORDS, NAMESPACES, PREPROCESSORS
from sqf.parser_types import Comment, Space, Tab, EndOfLine, BrokenEndOfLine, EndOfFile
from sqf.interpreter_types import DefineStatement, DefineResult
from sqf.parser_exp import parse_exp


def get_coord(tokens):
    return sqf.base_type.get_coord(''.join([str(x) for x in tokens]))


def identify_token(token):
    """
    The function that converts a token from tokenize to a BaseType.
    """
    if isinstance(token, (Comment, String)):
        return token
    if token == ' ':
        return Space()
    if token == '\t':
        return Tab()
    if token == '\\\n':
        return BrokenEndOfLine()
    if token in ('\n', '\r\n'):
        return EndOfLine(token)
    if token in ('true', 'false'):
        return Boolean(token == 'true')
    try:
        return Number(int(token))
    except ValueError:
        pass
    try:
        return Number(float(token))
    except ValueError:
        pass
    if token in PREPROCESSORS:
        return Preprocessor(token)
    if token.lower() in NAMESPACES:
        return Namespace(token)
    elif token.lower() in KEYWORDS:
        return Keyword(token)
    else:
        return Variable(token)


def replace_in_expression(expression, args, arg_indexes, all_tokens):
    """
    Recursively replaces matches of `args` in expression (a list of Types).
    """
    replacing_expression = []
    for token in expression:
        if isinstance(token, Statement):
            new_expression = replace_in_expression(token, args, arg_indexes, all_tokens)
            token = Statement(new_expression, ending=token.ending, parenthesis=token.parenthesis)
            replacing_expression.append(token)
        else:
            for arg, arg_index in zip(args, arg_indexes):
                if str(token) == arg:
                    replacing_expression.append(all_tokens[arg_index])
                    break
            else:
                replacing_expression.append(token)
    return replacing_expression


def parse_strings_and_comments(all_tokens):
    """
    Function that parses the strings of a script, transforming them into `String`.
    """
    string = ''  # the buffer for the activated mode
    tokens = []  # the final result
    in_double = False
    mode = None  # [None, "string_single", "string_double", "comment_line", "comment_bulk"]

    for i, token in enumerate(all_tokens):
        if mode == "string_double":
            string += token
            if token == '"':
                if in_double:
                    in_double = False
                elif not in_double and i != len(all_tokens) - 1 and all_tokens[i+1] == '"':
                    in_double = True
                else:
                    tokens.append(String(string))
                    mode = None
                    in_double = False
        elif mode == "string_single":
            string += token
            if token == "'":
                if in_double:
                    in_double = False
                elif not in_double and i != len(all_tokens) - 1 and all_tokens[i + 1] == "'":
                    in_double = True
                else:
                    tokens.append(String(string))
                    mode = None
                    in_double = False
        elif mode == "comment_bulk":
            string += token
            if token == '*/':
                mode = None
                tokens.append(Comment(string))
                string = ''
        elif mode == "comment_line":
            string += token
            if token in ('\n', '\r\n'):
                mode = None
                tokens.append(Comment(string))
                string = ''
        else:  # mode is None
            if token == '"':
                string = token
                mode = "string_double"
            elif token == "'":
                string = token
                mode = "string_single"
            elif token == '/*':
                string = token
                mode = "comment_bulk"
            elif token == '//':
                string = token
                mode = "comment_line"
            else:
                tokens.append(token)

    if mode in ("comment_line", "comment_bulk"):
        tokens.append(Comment(string))
    elif mode is not None:
        raise SQFParserError(get_coord(tokens), 'String is not closed')

    return tokens


def _analyze_tokens(tokens):
    ending = ''
    if tokens and tokens[-1] in (Keyword(';'), Keyword(',')):
        ending = tokens[-1].value
        del tokens[-1]

    statement = parse_exp(tokens, container=Statement)
    if isinstance(statement, Statement):
        statement._ending = ending
    else:
        statement = Statement([statement], ending=ending)

    return statement


def _analyze_array_tokens(tokens, tokens_until):
    result = []
    part = []
    first_comma_found = False
    for token in tokens:
        if token == Keyword(','):
            first_comma_found = True
            if not part:
                raise SQFParserError(get_coord(tokens_until), 'Array cannot have an empty element')
            result.append(_analyze_tokens(part))
            part = []
        else:
            part.append(token)

    # an empty array is a valid array
    if part == [] and first_comma_found:
        raise SQFParserError(get_coord(tokens_until), 'Array cannot have an empty element')
    elif tokens:
        result.append(_analyze_tokens(part))
    return result


def _analyze_define(tokens):
    assert(tokens[0] == Preprocessor('#define'))

    ending = ''
    if type(tokens[-1]) in (EndOfLine, Comment):
        ending = str(tokens[-1])
        del tokens[-1]

    valid_indexes = [i for i in range(len(tokens)) if not isinstance(tokens[i], ParserType)]

    if len(valid_indexes) < 2:
        raise SQFParserError(get_coord(str(tokens[0])), '#define needs at least one argument')
    variable = str(tokens[valid_indexes[1]])
    if len(valid_indexes) == 2:
        return DefineStatement(tokens, variable, ending=ending)
    elif len(valid_indexes) >= 3 and valid_indexes[1] + 1 == valid_indexes[2] and isinstance(tokens[valid_indexes[2]], Statement) and tokens[valid_indexes[2]].parenthesis:
        args = str(tokens[valid_indexes[2]])[1:-1].split(',')
        remaining = tokens[valid_indexes[3]:]
        return DefineStatement(tokens, variable, remaining, args=args, ending=ending)
    elif len(valid_indexes) >= 3:
        remaining = tokens[valid_indexes[2]:]
        return DefineStatement(tokens, variable, remaining, ending=ending)


def parse_block(all_tokens, analyze_tokens, analyze_array, start=0, initial_lvls=None, stop_statement='both', defines=None):
    if not initial_lvls:
        initial_lvls = {'[]': 0, '()': 0, '{}': 0}
        initial_lvls.update({x: 0 for x in PREPROCESSORS})
    if defines is None:
        defines = defaultdict(dict)
    lvls = initial_lvls.copy()

    statements = []
    tokens = []
    i = start

    while i < len(all_tokens):
        token = all_tokens[i]

        # try to match an expression and get the arguments
        found = False
        if str(token) in defines:  # is a define
            if i + 1 < len(all_tokens) and str(all_tokens[i + 1]) == '(':
                possible_args = defines[str(token)]
                arg_indexes = []
                for arg_number in possible_args:
                    if arg_number == 0:
                        continue

                    for arg_i in range(arg_number + 1):
                        if arg_i == arg_number:
                            index = i + 2 + 2*arg_i - 1
                        else:
                            index = i + 2 + 2 * arg_i

                        if index >= len(all_tokens):
                            break
                        arg_str = str(all_tokens[index])

                        if arg_i == arg_number and arg_str != ')':
                            break
                        elif not re.match('(.*?)', arg_str):
                            break
                        if arg_i != arg_number:
                            arg_indexes.append(index)
                    else:
                        define_statement = defines[str(token)][arg_number]
                        found = True
                        break
            elif 0 in defines[str(token)]:
                define_statement = defines[str(token)][0]
                arg_indexes = []
                found = True

            if found:
                arg_number = len(define_statement.args)

                extra_tokens_to_move = 1 + 2*(arg_number != 0)+2*arg_number - 1*(arg_number != 0)

                replaced_expression = all_tokens[i:i+extra_tokens_to_move]

                # the `all_tokens` after replacement
                replacing_expression = replace_in_expression(define_statement.expression, define_statement.args, arg_indexes, all_tokens)

                new_all_tokens = all_tokens[:i - len(tokens)] + tokens + replacing_expression + all_tokens[i + extra_tokens_to_move:]

                new_start = i - len(tokens)

                expression, size = parse_block(new_all_tokens, analyze_tokens, analyze_array, new_start, lvls, stop_statement, defines=defines)

                # the all_tokens of the statement before replacement
                original_tokens_taken = len(replaced_expression) - len(replacing_expression) + size

                original_tokens = all_tokens[i-len(tokens):i-len(tokens) + original_tokens_taken]

                if isinstance(expression, Statement):
                    expression = expression[0]

                if type(original_tokens[-1]) in (EndOfLine, Comment, EndOfFile):
                    del original_tokens[-1]
                    original_tokens_taken -= 1

                expression = DefineResult(original_tokens, define_statement, expression)
                statements.append(expression)

                i += original_tokens_taken - len(tokens) - 1

                tokens = []
        if found:
            pass
        elif token == Keyword('['):
            lvls['[]'] += 1
            expression, size = parse_block(all_tokens, analyze_tokens, analyze_array, i + 1, lvls, stop_statement='single', defines=defines)
            lvls['[]'] -= 1
            tokens.append(expression)
            i += size + 1
        elif token == Keyword('('):
            lvls['()'] += 1
            expression, size = parse_block(all_tokens, analyze_tokens, analyze_array, i + 1, lvls, stop_statement, defines=defines)
            lvls['()'] -= 1
            tokens.append(expression)
            i += size + 1
        elif token == Keyword('{'):
            lvls['{}'] += 1
            expression, size = parse_block(all_tokens, analyze_tokens, analyze_array, i + 1, lvls, stop_statement, defines=defines)
            lvls['{}'] -= 1
            tokens.append(expression)
            i += size + 1

        elif token == Keyword(']'):
            if lvls['[]'] == 0:
                raise SQFParenthesisError(get_coord(all_tokens[:i]), 'Trying to close right parenthesis without them opened.')

            if statements:
                if isinstance(statements[0], DefineResult):
                    statements[0]._tokens = [Array(analyze_array(statements[0]._tokens, all_tokens[:i]))]#[Array(statements[0]._tokens)]
                    return statements[0], i - start
                else:
                    raise SQFParserError(get_coord(all_tokens[:i]), 'A statement %s cannot be in an array' % Statement(statements))

            return Array(analyze_array(tokens, all_tokens[:i])), i - start
        elif token == Keyword(')'):
            if lvls['()'] == 0:
                raise SQFParenthesisError(get_coord(all_tokens[:i]), 'Trying to close parenthesis without opened parenthesis.')

            if tokens:
                statements.append(analyze_tokens(tokens))

            return Statement(statements, parenthesis=True), i - start
        elif token == Keyword('}'):
            if lvls['{}'] == 0:
                raise SQFParenthesisError(get_coord(all_tokens[:i]), 'Trying to close brackets without opened brackets.')

            if tokens:
                statements.append(analyze_tokens(tokens))

            return Code(statements), i - start
        elif all(lvls[x] == 0 for x in PREPROCESSORS) and \
                stop_statement == 'both' and token in (Keyword(';'), Keyword(',')) or \
                stop_statement == 'single' and token == Keyword(';'):
            tokens.append(token)
            statements.append(analyze_tokens(tokens))
            tokens = []
        elif isinstance(token, Keyword) and token.value in PREPROCESSORS:
            # notice that `token` is ignored here. It will be picked up in the end
            if tokens:
                # a pre-processor starts a new statement
                statements.append(analyze_tokens(tokens))
                tokens = []

            lvls[token.value] += 1
            expression, size = parse_block(all_tokens, analyze_tokens, analyze_array, i + 1, lvls, stop_statement, defines=defines)
            lvls[token.value] -= 1

            statements.append(expression)
            i += size + 1
        elif type(token) in (EndOfLine, Comment, EndOfFile) and any(lvls[x] != 0 for x in PREPROCESSORS):
            tokens.insert(0, all_tokens[start - 1])  # pick the token that triggered the statement
            if type(token) != EndOfFile:
                tokens.append(token)
            if tokens[0] == Preprocessor('#define'):
                define_statement = _analyze_define(tokens)
                defines[define_statement.variable_name][len(define_statement.args)] = define_statement
                statements.append(define_statement)
            else:
                statements.append(analyze_tokens(tokens))

            return Statement(statements), i - start
        elif type(token) != EndOfFile:
            tokens.append(token)
        i += 1

    for lvl_type in lvls:
        if lvls[lvl_type] != 0 and lvl_type not in PREPROCESSORS:
            raise SQFParenthesisError(get_coord(all_tokens[:start - 1]), 'Parenthesis "%s" not closed' % lvl_type[0])

    if tokens:
        statements.append(analyze_tokens(tokens))

    return Statement(statements), i - start


def parse(script):
    tokens = [identify_token(x) for x in parse_strings_and_comments(tokenize(script))]

    result = parse_block(tokens + [EndOfFile()], _analyze_tokens, _analyze_array_tokens)[0]

    result.set_position((1, 1))

    return result
