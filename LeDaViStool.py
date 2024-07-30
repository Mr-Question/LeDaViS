import sys
import datetime
import os.path
import json
import re
import textwrap

from collections import defaultdict

from lark import Lark, Transformer, Tree, Token
from lark.exceptions import UnexpectedToken, UnexpectedCharacters

from pyvis.network import Network


grammar = r"""
file: ISO ";" header data_section "END-" ISO ";"
header: "HEADER" ";" header_entity_list "ENDSEC" ";"
header_entity_list: header_entity header_entity*
header_entity :keyword "(" parameter_list ")" ";"

data_section: "DATA" ";" (entity_instance)* "ENDSEC" ";"
entity_instance: simple_entity_instance|complex_entity_instance 
simple_entity_instance: id "=" simple_record ";" 
complex_entity_instance: id "=" subsuper_record ";"
subsuper_record : "(" simple_record_list ")" 
simple_record_list:simple_record simple_record* 
simple_record: keyword "("parameter_list?")"

keyword: /[A-Z][0-9A-Z_]*/

parameter_list: parameter ("," parameter)*
parameter: typed_parameter|untyped_parameter|omitted_parameter
typed_parameter: keyword "(" parameter ")"|"()" 
untyped_parameter: string| NONE |INT |REAL |enumeration |id |binary |list
omitted_parameter:STAR

ISO: "ISO-" TOKEN "-" TOKEN
TOKEN: (DIGIT|LOWER|UPPER)+ 

id: /#[0-9]+/
binary: "\"" ("0"|"1"|"2"|"3") (HEX)* "\"" 
list: "(" parameter ("," parameter)* ")" |"("")"
enumeration: "." keyword "."
string: "'" (REVERSE_SOLIDUS REVERSE_SOLIDUS|WS|SPECIAL|DIGIT|LOWER|UPPER|CONTROL_DIRECTIVE|"\\*\\")* "'" 

REAL: SIGN?  DIGIT  (DIGIT)* "." (DIGIT)* ("E"  SIGN?  DIGIT (DIGIT)* )?
INT: SIGN? DIGIT  (DIGIT)* 

STAR           : "*"
SLASH          : "/"
NONE           : "$"
APOSTROPHE     : "'"
REVERSE_SOLIDUS: "\\"

DIGIT          : "0".."9"
SIGN           : "+"|"-"
LOWER          : "a".."z"
UPPER          : "A".."Z"
ESCAPE         : "\\" ( "$" | "\"" | CHAR )
CHAR           : /[^$"\n]/
WORD           : CHAR+

HEX_FOUR: HEX_TWO HEX_TWO
HEX_TWO: HEX_ONE HEX_ONE 
HEX_ONE: HEX HEX
HEX:      "0" 
        | "1" 
        | "2" 
        | "3" 
        | "4" 
        | "5"
        | "6" 
        | "7" 
        | "8" 
        | "9" 
        | "A" 
        | "B" 
        | "C" 
        | "D" 
        | "E" 
        | "F" 

SPECIAL : "!"  
        | "*"
        | "$" 
        | "%" 
        | "&" 
        | "." 
        | "#" 
        | "+" 
        | "," 
        | "-" 
        | "(" 
        | ")" 
        | "?" 
        | "/" 
        | ":" 
        | ";" 
        | "<" 
        | "=" 
        | ">" 
        | "@" 
        | "[" 
        | "]" 
        | "{" 
        | "|" 
        | "}" 
        | "^" 
        | "`" 
        | "~"
        | "_"
        | "\""
        | "\"\""
        | "''"
        
CONTROL_DIRECTIVE: PAGE | ALPHABET | EXTENDED2 | EXTENDED4 | ARBITRARY 
PAGE : REVERSE_SOLIDUS "S" REVERSE_SOLIDUS LATIN_CODEPOINT
LATIN_CODEPOINT : DIGIT | LOWER | UPPER | SPECIAL | REVERSE_SOLIDUS | APOSTROPHE
ALPHABET : REVERSE_SOLIDUS "P" UPPER REVERSE_SOLIDUS 
EXTENDED2: REVERSE_SOLIDUS "X2" REVERSE_SOLIDUS (HEX_TWO)* END_EXTENDED 
EXTENDED4 :REVERSE_SOLIDUS "X4" REVERSE_SOLIDUS (HEX_FOUR)* END_EXTENDED 
END_EXTENDED: REVERSE_SOLIDUS "X0" REVERSE_SOLIDUS 
ARBITRARY: REVERSE_SOLIDUS "X" REVERSE_SOLIDUS HEX_ONE 

WS: /[ \t\f\r\n]/+
%ignore /[\n]/+
"""

###############################################################################
# ValidationError
###############################################################################
class ValidationError(Exception):
    pass


###############################################################################
# SyntaxError
###############################################################################
class SyntaxError(ValidationError):
    def __init__(self, filecontent, exception):
        self.filecontent = filecontent
        self.exception = exception

    def asdict(self, with_message=True):
        return {
            "type": (
                "unexpected_token"
                if isinstance(self.exception, UnexpectedToken)
                else "unexpected_character"
            ),
            "lineno": self.exception.line,
            "column": self.exception.column,
            "found_type": self.exception.token.type.lower(),
            "found_value": self.exception.token.value,
            "expected": sorted(x for x in self.exception.accepts if "__ANON" not in x),
            "line": self.filecontent.split("\n")[self.exception.line - 1],
            **({"message": str(self)} if with_message else {}),
        }

    def __str__(self):
        d = self.asdict(with_message=False)
        if len(d["expected"]) == 1:
            exp = d["expected"][0]
        else:
            exp = f"one of {' '.join(d['expected'])}"

        sth = "character" if d["type"] == "unexpected_character" else ""

        return f"On line {d['lineno']} column {d['column']}:\nUnexpected {sth}{d['found_type']} ('{d['found_value']}')\nExpecting {exp}\n{d['lineno']:05d} | {d['line']}\n        {' ' * (self.exception.column - 1)}^"


###############################################################################
# DuplicateNameError
###############################################################################
class DuplicateNameError(ValidationError):
    def __init__(self, filecontent, name, linenumbers):
        self.name = name
        self.filecontent = filecontent
        self.linenumbers = linenumbers

    def asdict(self, with_message=True):
        return {
            "type": "duplicate_name",
            "name": self.name,
            "lineno": self.linenumbers[0],
            "line": self.filecontent.split("\n")[self.linenumbers[0] - 1],
            **({"message": str(self)} if with_message else {}),
        }

    def __str__(self):
        d = self.asdict(with_message=False)

        def build():
            yield f"On line {d['lineno']}:\nDuplicate instance name {d['name']}"
            yield f"{d['lineno']:05d} | {d['line']}"
            yield " " * 8 + "^" * len(d["line"].rstrip())

        return "\n".join(build())


###############################################################################
# Transformer
###############################################################################
class T(Transformer):
    def id(self, s):
        return s[0]

    def string(self, s):
        word = "".join(s)
        return word

    def keyword(self, s):
        word = "".join(s)
        return word

    def untyped_parameter(self, s):
        return s[0] if len(s) > 0 else ''

    def typed_parameter(self, s):
        return s[0] if len(s) > 0 else ''
    
    def parameter(self, s):
        return s[0] if len(s) > 0 else ''

    def omitted_parameter(self, s):
        return s[0]

    def enumeration(self, s):
        return s[0]

    parameter_list     = tuple
    list               = list
    simple_record      = tuple
    subsuper_record    = tuple
    simple_record_list = tuple
    INT                = int
    REAL               = float
    NONE               = str
    STAR               = str
    

###############################################################################
# explore_data
###############################################################################
DATA_TAG  = 'data'    
BODY_TAG  = 'body'
REFS_TAG  = 'refs'
LINES_TAG = 'lines'

def explore_data(filecontent, data_tree):
    
    def get_line_number(t):
        if isinstance(t, Token):
            yield t.line

    def traverse(fn, x):
        yield from fn(x)
        if isinstance(x, Tree):
            for c in x.children:
                yield from traverse(fn, c)
    
    objects = defaultdict(list)
    for entity in data_tree.children:
        lines = list(traverse(get_line_number, entity))
        
        entity_instance = entity.children[0]
        
        id     = str(entity_instance.children[0])
        record = entity_instance.children[1]
        
        def explode_object(t, refs: list):
            a = list()
            if isinstance(t, list) or isinstance(t, tuple):
                for x in t:
                    a.append(explode_object(x, refs))                
            elif isinstance(t, Tree):
                for x in t.children:
                    a.append(explode_object(x, refs))
            elif isinstance(t, Token):
                s = str(t)
                a = s
                refs.append(s)
            else:
                s = str(t)
                a = s
            return a
        
        refs = []
        body = explode_object(record, refs)
        
        lines_range = (min(lines), max(lines))
        if objects[id]:
            raise DuplicateNameError(filecontent, id, lines_range)
        
        object = {
            BODY_TAG : body,
            REFS_TAG : refs,
            LINES_TAG: lines_range
        }
        
        objects[id].append(object)
        
    return objects
    

###############################################################################
# explore_model
###############################################################################
def explore_model(filecontent, entity_tree):
    t = T(visit_tokens=True).transform(entity_tree)
    
    data = explore_data(filecontent, t.children[2])

    return {
        DATA_TAG: data
    }


###############################################################################
# process_tree
###############################################################################
def process_tree(filecontent, file_tree, with_progress):
    return explore_model(filecontent, file_tree)


###############################################################################
# parse
###############################################################################
def parse(filecontent, with_progress):
    
    def replace_fn(match):
        return re.sub(r"[^\n]", " ", match.group(), flags=re.M)

    # Match and remove the comments
    p = r"/\*[\s\S]*?\*/"
    filecontent_wo_comments = re.sub(p, replace_fn, filecontent)
    
    # Match whitespaces except in the strings
    p = r"(#[\d]+|'[^']*')|([\r\t\f\v ]*)"
    filecontent_wo_comments = re.sub(p, r"\g<1>", filecontent_wo_comments)
    
    transformer = {}
    parser = Lark(grammar, parser="lalr", start="file", **transformer)
    
    try:
        ast = parser.parse(filecontent_wo_comments)
    except (UnexpectedToken, UnexpectedCharacters) as e:
        raise SyntaxError(filecontent, e)

    return process_tree(filecontent, ast, with_progress)


###############################################################################
# read
###############################################################################
def read(*, filename=None, with_progress=False):
    filecontent = open(filename, encoding=None).read()
    
    return parse(filecontent, with_progress)


###############################################################################
# add_node
###############################################################################
SPLIT_STRING_SIZE  = 100

ENTRY_NODE_COLOR   = 'indigo'
FAILURE_NODE_COLOR = 'red'

NODE_COLOR = {
#DISPLAY ENTITIES STEP
  "CARTESIAN_POINT"            : 'lightgrey',
  'PCURVE'                     : 'orange',
  'B_SPLINE_CURVE_WITH_KNOTS'  : 'palegreen',
  'B_SPLINE_SURFACE_WITH_KNOTS': 'darkkhaki',
  
#DISPLAY ENTITIES IFC
  'IFCCARTESIANPOINT'          : 'lightgrey',
  'IFCPOLYLINE'                : 'orange',
  'IFCSHAPEREPRESENTATION'     : 'darkkhaki'
}

def add_node(net, node, data, force_color=''):
    node_color = ''
    if len(force_color) > 0:
        node_color = force_color
    else:
        if net.num_nodes() == 0:
            node_color = ENTRY_NODE_COLOR
            
    net.add_node(node)
        
    def to_str(object):
        if isinstance(object, str):
            yield object
        else:
            a = list()
            for x in object:
                s = ", ".join(k for k in to_str(x))
                a.append(s if s != '' else '\'\'')
                
            s = ", ".join(k for k in a)
            yield "(" + s + ")"
            
    def simple_entity(body, color):
        name  = list(to_str(body[1]) if len(body) > 1 else {"()"})
        
        name = body[0] + str(name[0])
        name = textwrap.wrap(name, SPLIT_STRING_SIZE)
        name = "<br>".join(name)
            
        if len(color) == 0:
            color = NODE_COLOR[body[0]] if body[0] in NODE_COLOR else {}
        
        return name, color
            
    
    title=''
    for object in data[node]:
        body = object[BODY_TAG]
        
        if isinstance(body[0], str):
            title, node_color = simple_entity(body, node_color)
        else:
            for x in body[0]:
                curr_title, node_color = simple_entity(x, node_color)
                title += ("<br>" if len(title) > 0 else "") + curr_title
    
    gnode = net.get_node(node)
    gnode['title'] = title
    if len(node_color) > 0:
        gnode['color'] = node_color


###############################################################################
# make_graph_complete
###############################################################################
def make_graph_complete(net, model):
    data = model[DATA_TAG]
    for node in data:
        add_node(net, node, data)
        
    for node in data:
        for object in data[node]:
            for ref in object[REFS_TAG]:
                if not ref in data:
                    add_node(net, ref, data, force_color=FAILURE_NODE_COLOR)
                net.add_edge(node, ref)


###############################################################################
# make_graph_entity
###############################################################################
def make_graph_entity(net, model, entity):
    data = model[DATA_TAG]
        
    nodes = defaultdict(set)
    
    entities = {entity}
    
    while len(entities) > 0:
        new_entities = set()
        
        for curr_entity in entities:
            if curr_entity in nodes:
                continue
            
            nodes[curr_entity]
            if curr_entity in data:
                add_node(net, curr_entity, data)
            else:
                add_node(net, curr_entity, data, force_color=FAILURE_NODE_COLOR)
                continue
            
            for object in data[curr_entity]:
                for ref in object[REFS_TAG]:
                    nodes[curr_entity].add(ref)
                    new_entities.add(ref)
            
        entities = new_entities
        
    for node in nodes:
        for ref in nodes[node]:
            net.add_edge(node, ref)


###############################################################################
# make_graph
###############################################################################
def make_graph(model, output_html_file, entity):
    net = Network(height='800px', width='100%', directed=True, notebook=False)
    
    if len(entity) == 0:
        make_graph_complete(net, model)
    else:
        make_graph_entity(net, model, entity)
    
    net.write_html(output_html_file)


###############################################################################
# __main__
###############################################################################
if __name__ == "__main__":
    args  = [x for x in sys.argv[1:] if not x.startswith("-")]
    flags = [x for x in sys.argv[1:] if     x.startswith("-")]

    if len(args) < 2:
        print("Error: parameters should be <input_step_or_ifc_file> <output_html_file> [entity] [--json]", file=sys.stderr)
        exit(1)
        
    if not os.path.isfile(args[0]):
        print("Error: No such file " + args[0], file=sys.stderr)
        exit(1)
        
    time_diff = datetime.datetime.now()

    ret_code=0
    try:
        model = read(filename=args[0], with_progress="--progress" in flags)
        make_graph(model, args[1], '#' + args[2] if len(args) > 2 else "")
    except ValidationError as e:
        if "--json" in flags:
            json.dump(e.asdict(), sys.stdout)
        else:
            print(e, file=sys.stderr)
        ret_code=1
    
    time_diff = datetime.datetime.now() - time_diff

    print(f"\nElapsed time: {time_diff}")    
    
    exit(ret_code)