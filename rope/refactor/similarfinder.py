"""This module can be used for finding similar code"""
import re

from rope.base import codeanalyze, evaluate, exceptions, pyobjects, ast
from rope.refactor import patchedast, sourceutils, occurrences


class SimilarFinder(object):
    """A class for finding similar expressions and statements"""

    def __init__(self, source, start=0, end=None):
        node = ast.parse(source)
        self._init_using_ast(node, source, start, end)

    def _init_using_ast(self, node, source, start, end):
        self.start = start
        self.end = len(source)
        if end is not None:
            self.end = end
        if not hasattr(node, 'sorted_children'):
            self.ast = patchedast.patch_ast(node, source)

    def get_matches(self, code):
        """Search for `code` in source and return a list of `Match`\es

        `code` can contain wildcards.  ``${name}`` matches normal
        names and ``${?name} can match any expression.  They can
        only appear in `_ast.Name`.
        You can use `Match.get_ast()` for getting the node that has
        matched a given pattern.
        """
        wanted = self._create_pattern(code)
        matches = _ASTMatcher(self.ast, wanted).find_matches()
        for match in matches:
            start, end = match.get_region()
            if self.start <= start and end <= self.end:
                yield match

    def get_match_regions(self, code):
        for match in self.get_matches(code):
            yield match.get_region()

    def _create_pattern(self, expression):
        expression = self._replace_wildcards(expression)
        node = ast.parse(expression)
        # Getting Module.Stmt.nodes
        nodes = node.body
        if len(nodes) == 1 and isinstance(nodes[0], ast.Expr):
            # Getting Discard.expr
            wanted = nodes[0].value
        else:
            wanted = nodes
        return wanted

    def _replace_wildcards(self, expression):
        ropevar = _RopeVariable()
        template = CodeTemplate(expression)
        mapping = {}
        for name in template.get_names():
            if name.startswith('?'):
                mapping[name] = ropevar.get_any(name)
            else:
                mapping[name] = ropevar.get_normal(name)
        return template.substitute(mapping)


class BadNameInCheckError(exceptions.RefactoringError):
    pass


class CheckingFinder(SimilarFinder):
    """A `SimilarFinder` that can perform object and name checks

    The constructor takes a `checks` dictionary.  This dictionary
    contains checks to be performed.  As an example::

      pattern: '${?a}.set(${?b})'
      checks: {'?a.type': type_pyclass}

      pattern: '${?c} = ${?C}())'
      checks: {'C': c_pyname}

    This means only match expressions as '?a' only if its type is
    type_pyclass.  Each matched expression is a `PyName`.  By using
    nothing, `.object` or `.type` you can specify a check.

    """

    def __init__(self, pymodule, checks, start=0, end=None):
        super(CheckingFinder, self)._init_using_ast(
            pymodule.get_ast(), pymodule.source_code, start, end)
        self.pymodule = pymodule
        self.checks = checks

    def get_matches(self, code):
        for match in SimilarFinder.get_matches(self, code):
            matched = True
            for check, expected in self.checks.items():
                name, kind = self._split_name(check)
                node = match.get_ast(name)
                if node is None:
                    raise BadNameInCheckError('Unknown name <%s>' % name)
                pyname = self._evaluate_node(node)
                if kind == 'name':
                    if not self._same_pyname(expected, pyname):
                        break
                else:
                    pyobject = pyname.get_object()
                    if kind == 'type':
                        pyobject = pyobject.get_type()
                    if not self._same_pyobject(expected, pyobject):
                        break
            else:
                yield match

    def _same_pyobject(self, expected, pyobject):
        return expected == pyobject

    def _same_pyname(self, expected, pyname):
        return occurrences.FilteredFinder.same_pyname(expected, pyname)

    def _split_name(self, name):
        parts = name.split('.')
        expression, kind = parts[0], parts[-1]
        if len(parts) == 1:
            kind = 'name'
        return expression, kind

    def _evaluate_node(self, node):
        scope = self.pymodule.get_scope().get_inner_scope_for_line(node.lineno)
        expression = node
        if isinstance(expression, ast.Name) and \
           isinstance(expression.ctx, ast.Store):
            start, end = patchedast.node_region(expression)
            text = self.pymodule.source_code[start:end]
            return evaluate.get_string_result(scope, text)
        else:
            return evaluate.get_statement_result(scope, expression)


class _ASTMatcher(object):

    def __init__(self, body, pattern):
        """Searches the given pattern in the body AST.

        body is an AST node and pattern can be either an AST node or
        a list of ASTs nodes
        """
        self.body = body
        self.pattern = pattern
        self.matches = None
        self.ropevar = _RopeVariable()

    def find_matches(self):
        if self.matches is None:
            self.matches = []
            ast.call_for_nodes(self.body, self._check_node, recursive=True)
        return self.matches

    def _check_node(self, node):
        if isinstance(self.pattern, list):
            self._check_statements(node)
        else:
            self._check_expression(node)

    def _check_expression(self, node):
        mapping = {}
        if self._match_nodes(self.pattern, node, mapping):
            self.matches.append(ExpressionMatch(node, mapping))

    def _check_statements(self, node):
        for child in ast.get_children(node):
            if isinstance(child, (list, tuple)):
                self.__check_stmt_list(child)

    def __check_stmt_list(self, nodes):
        for index in range(len(nodes)):
            if len(nodes) - index >= len(self.pattern):
                current_stmts = nodes[index:index + len(self.pattern)]
                mapping = {}
                if self._match_stmts(current_stmts, mapping):
                    self.matches.append(StatementMatch(current_stmts, mapping))

    def _match_nodes(self, expected, node, mapping):
        if isinstance(expected, ast.Name):
           if self.ropevar.is_normal(expected.id):
               return self._match_normal_var(expected, node, mapping)
           if self.ropevar.is_any(expected.id):
               return self._match_any_var(expected, node, mapping)
        if not isinstance(expected, ast.AST):
            return expected == node
        if expected.__class__ != node.__class__:
            return False

        children1 = ast.get_children(expected)
        children2 = ast.get_children(node)
        if len(children1) != len(children2):
            return False
        for child1, child2 in zip(children1, children2):
            if isinstance(child1, ast.AST):
                if not self._match_nodes(child1, child2, mapping):
                    return False
            elif isinstance(child1, (list, tuple)):
                if not isinstance(child2, (list, tuple)):
                    return False
                for c1, c2 in zip(child1, child2):
                    if not self._match_nodes(c1, c2, mapping):
                        return False
            else:
                if child1 != child2:
                    return False
        return True

    def _match_stmts(self, current_stmts, mapping):
        if len(current_stmts) != len(self.pattern):
            return False
        for stmt, expected in zip(current_stmts, self.pattern):
            if not self._match_nodes(expected, stmt, mapping):
                return False
        return True

    def _match_normal_var(self, node1, node2, mapping):
        if node2.__class__ == node1.__class__ and \
           self.ropevar.get_base(node1.id) == node2.id:
            mapping[self.ropevar.get_base(node1.id)] = node2
            return True
        return False

    def _match_any_var(self, node1, node2, mapping):
        name = self.ropevar.get_base(node1.id)
        if name not in mapping:
            mapping[name] = node2
            return True
        else:
            return self._match_nodes(mapping[name], node2, {})


class Match(object):

    def __init__(self, mapping):
        self.mapping = mapping

    def get_region(self):
        """Returns match region"""

    def get_ast(self, name):
        """The ast node that has matched rope variables"""
        return self.mapping.get(name, None)

class ExpressionMatch(Match):

    def __init__(self, ast, mapping):
        super(ExpressionMatch, self).__init__(mapping)
        self.ast = ast

    def get_region(self):
        return self.ast.region


class StatementMatch(Match):

    def __init__(self, ast_list, mapping):
        super(StatementMatch, self).__init__(mapping)
        self.ast_list = ast_list

    def get_region(self):
        return self.ast_list[0].region[0], self.ast_list[-1].region[1]


class CodeTemplate(object):

    def __init__(self, template):
        self.template = template
        self._find_names()

    def _find_names(self):
        self.names = {}
        for match in CodeTemplate._get_pattern().finditer(self.template):
            if 'name' in match.groupdict() and \
               match.group('name') is not None:
                start, end = match.span('name')
                name = self.template[start + 2:end - 1]
                if name not in self.names:
                    self.names[name] = []
                self.names[name].append((start, end))

    def get_names(self):
        return self.names.keys()

    def substitute(self, mapping):
        collector = sourceutils.ChangeCollector(self.template)
        for name, occurrences in self.names.items():
            for region in occurrences:
                collector.add_change(region[0], region[1], mapping[name])
        result = collector.get_changed()
        if result is None:
            return self.template
        return result

    _match_pattern = None

    @classmethod
    def _get_pattern(cls):
        if cls._match_pattern is None:
            pattern = codeanalyze.get_comment_pattern() + '|' + \
                      codeanalyze.get_string_pattern() + '|' + \
                      r'(?P<name>\$\{[^\s\$]*\})'
            cls._match_pattern = re.compile(pattern)
        return cls._match_pattern


class _RopeVariable(object):
    """Transform and identify rope inserted wildcards"""

    _normal_prefix = '__rope__variable_normal_'
    _any_prefix = '__rope_variable_any_'

    def get_normal(self, name):
        return self._normal_prefix + name

    def get_any(self, name):
        return self._any_prefix + name[1:]

    def is_normal(self, name):
        return name.startswith(self._normal_prefix)

    def is_any(self, name):
        return name.startswith(self._any_prefix)

    def get_base(self, name):
        if self.is_normal(name):
            return name[len(self._normal_prefix):]
        if self.is_any(name):
            return '?' + name[len(self._any_prefix):]
