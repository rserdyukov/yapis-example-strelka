// Грамматика языка FSM — языка описания конечных автоматов.
//
// Тот же язык описан второй раз для Lark: fsmc/frontend_lark/fsm.lark.
// Обе грамматики обязаны принимать одни и те же программы и строить
// одинаковое AST — это проверяет tests/test_frontends.py.
//
// Сгенерировать парсер (результат закоммичен, Java в CI и в браузере не нужна):
//   antlr4 -Dlanguage=Python3 -visitor -no-listener \
//          -o fsmc/frontend_antlr/generated -Xexact-output-dir grammar/Fsm.g4
// или просто: python3 tools/gen_antlr.py
//
// Переводы строк незначимы. Чтобы грамматика оставалась однозначной без них,
// переход в себя записывается только с действием: `A -- e -> / act`
// (после `->` идёт либо имя состояния, либо `/`). По той же причине оператор
// вызова действия в блоке пишется со скобками: `show(h)`, а не `show`.

grammar Fsm;

// Файл — либо один автомат (`machine`), либо модуль-библиотека (`module`),
// либо система из нескольких автоматов (`system`). Внутри module и system
// автоматы записываются в фигурных скобках и могут иметь параметры.
program
    : 'machine' name=ID decl* EOF                         # machineFile
    | kind=('module' | 'system') name=ID unitDecl* EOF    # unitFile
    ;

unitDecl
    : importDecl
    | constDecl
    | funcDecl
    | actionDecl
    | interfaceDecl
    | machineDecl
    | instanceDecl
    | inputDecl
    ;

decl
    : importDecl
    | constDecl
    | contextDecl
    | eventsDecl
    | statesDecl
    | initialDecl
    | terminalDecl
    | actionDecl
    | funcDecl
    | transition
    ;

// ---------------------------------------------------------------- объявления

importDecl   : 'import' ID (',' ID)* ;

constDecl    : 'const' name=ID ':' ty=typeRef '=' expr ;

contextDecl  : 'context' '{' (field (','? field)*)? '}' ;
field        : name=ID ':' ty=typeRef ('=' expr)? ;

eventsDecl   : 'events' '{' (eventSig (','? eventSig)*)? '}' ;
eventSig     : name=ID ('(' params? ')')? ;

params       : param (',' param)* ;
param        : byref='var'? name=ID ':' ty=typeRef ;

// int, int[52], Seat (интерфейс), poker.Seat (из модуля)
typeRef      : (mod=ID '.')? name=ID ('[' size=INT ']')? ;

statesDecl   : 'states' idList ;
initialDecl  : 'initial' idList ;
terminalDecl : 'terminal' idList ;
idList       : ID (',' ID)* ;

actionDecl   : 'action' name=ID ('(' params? ')')? block ;
funcDecl     : 'func' name=ID '(' params? ')' ARROW ret=typeRef block ;

// ---------------------------------------------------------------- композиция

machineDecl   : 'machine' name=ID ('(' params? ')')? '{' decl* '}' ;
interfaceDecl : 'interface' name=ID '{' (eventSig (','? eventSig)*)? '}' ;
//   table: Table([you, bot])      bot: poker.Bot(table, 2)
instanceDecl  : name=ID ':' (mod=ID '.')? machine=ID ('(' (expr (',' expr)*)? ')')? ;
//   input table.start, you        — события, которые система принимает извне
inputDecl     : 'input' inputRef (',' inputRef)* ;
inputRef      : inst=ID ('.' event=ID)? ;

// ---------------------------------------------------------------- переходы

//   PinWait -- pin(c) [c == PIN]      -> Menu     / greet
//   PinWait -- pin(_) else            -> / count_attempt
//   Idle    -- pin | cancel               ignore
//   any     -- card(c)                -> / take(c)     в любом состоянии
//   Idle    -- any                        ignore       все прочие события
//   Flop | Turn -- bet(x)             -> / take(x)     в нескольких состояниях
transition
    : sources DASHES triggers guard? ARROW target=ID calls?  # moveTransition
    | sources DASHES triggers guard? ARROW calls             # selfTransition
    | sources DASHES triggers 'ignore'                       # ignoreTransition
    ;

sources      : source ('|' source)* ;
source       : ID | 'any' ;

triggers     : trigger ('|' trigger)* ;
trigger      : event=(ID | 'any') ('(' (pattern (',' pattern)*)? ')')? ;
pattern      : name=ID (':' ty=ID)? ;

guard
    : 'else'? '[' expr ']'
    | 'else'
    ;

calls        : '/' call (',' call)* ;
call
    : 'send' sendTarget                                       # sendCall
    | (mod=ID '.')? name=ID ('(' (expr (',' expr)*)? ')')?    # actionCall
    ;
//   table.bet(10)    seats[i].deal(c)    self.next()
sendTarget   : target=ID ('[' index=expr ']')? '.' event=ID ('(' (expr (',' expr)*)? ')')? ;

// ---------------------------------------------------------------- операторы

block        : '{' stmt* '}' ;

stmt
    : varStmt
    | assignStmt
    | sayStmt
    | ifStmt
    | forStmt
    | returnStmt
    | sendStmt
    | callStmt
    ;
varStmt      : 'var' name=ID ':' ty=typeRef ('=' expr)? ;
assignStmt   : target=ID ('[' expr ']')? '=' expr ;
// say — строка целиком, write — без перевода строки
sayStmt      : kw=('say' | 'write') expr (',' expr)* ;
ifStmt       : 'if' expr block ('else' (block | ifStmt))? ;
// i пробегает start, start+1, ..., end-1; границы вычисляются один раз
forStmt      : 'for' var=ID 'in' expr '..' expr block ;
returnStmt   : 'return' expr ;
sendStmt     : 'send' sendTarget ;
callStmt     : (mod=ID '.')? name=ID '(' (expr (',' expr)*)? ')' ;

// ---------------------------------------------------------------- выражения
// Приоритет — сверху вниз по убыванию. `not` ниже сравнений:
// `not a == b` означает `not (a == b)`.

expr
    : '(' expr ')'                                       # parenExpr
    | op='-' expr                                        # negExpr
    | expr op=('*' | '/' | '%') expr                     # binExpr
    | expr op=('+' | '-') expr                           # binExpr
    | expr op=('==' | '!=' | '<' | '<=' | '>' | '>=') expr  # binExpr
    | op='not' expr                                      # notExpr
    | expr op='and' expr                                 # binExpr
    | expr op='or' expr                                  # binExpr
    | INT                                                # intExpr
    | STRING                                             # strExpr
    | value=('true' | 'false')                           # boolExpr
    | '[' (expr (',' expr)*)? ']'                        # arrayExpr
    | (mod=ID '.')? name=ID '(' (expr (',' expr)*)? ')'  # callExpr
    | (mod=ID '.')? name=ID '[' expr ']'                 # indexExpr
    | (mod=ID '.')? name=ID                              # refExpr
    ;

// ---------------------------------------------------------------- лексика

// «Стрелки» любой длины, как в эскизе: `----- pin ------->`.
// ARROW длиннее DASHES на `>`, поэтому `--->` всегда стрелка.
ARROW   : '-'+ '>' ;
DASHES  : '--' '-'* ;

ID      : [\p{L}_] [\p{L}\p{Nd}_]* ;
INT     : [0-9]+ ;
STRING  : '"' (~["\\\r\n] | '\\' ["\\n])* '"' ;

COMMENT : '//' ~[\r\n]* -> skip ;
WS      : [ \t\r\n]+ -> skip ;
