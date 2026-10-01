# Generated from Fsm.g4 by ANTLR 4.13.2
from antlr4 import *
if "." in __name__:
    from .FsmParser import FsmParser
else:
    from FsmParser import FsmParser

# This class defines a complete generic visitor for a parse tree produced by FsmParser.

class FsmVisitor(ParseTreeVisitor):

    # Visit a parse tree produced by FsmParser#machineFile.
    def visitMachineFile(self, ctx:FsmParser.MachineFileContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#unitFile.
    def visitUnitFile(self, ctx:FsmParser.UnitFileContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#unitDecl.
    def visitUnitDecl(self, ctx:FsmParser.UnitDeclContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#decl.
    def visitDecl(self, ctx:FsmParser.DeclContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#importDecl.
    def visitImportDecl(self, ctx:FsmParser.ImportDeclContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#constDecl.
    def visitConstDecl(self, ctx:FsmParser.ConstDeclContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#contextDecl.
    def visitContextDecl(self, ctx:FsmParser.ContextDeclContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#field.
    def visitField(self, ctx:FsmParser.FieldContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#eventsDecl.
    def visitEventsDecl(self, ctx:FsmParser.EventsDeclContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#eventSig.
    def visitEventSig(self, ctx:FsmParser.EventSigContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#params.
    def visitParams(self, ctx:FsmParser.ParamsContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#param.
    def visitParam(self, ctx:FsmParser.ParamContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#typeRef.
    def visitTypeRef(self, ctx:FsmParser.TypeRefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#statesDecl.
    def visitStatesDecl(self, ctx:FsmParser.StatesDeclContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#initialDecl.
    def visitInitialDecl(self, ctx:FsmParser.InitialDeclContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#terminalDecl.
    def visitTerminalDecl(self, ctx:FsmParser.TerminalDeclContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#idList.
    def visitIdList(self, ctx:FsmParser.IdListContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#actionDecl.
    def visitActionDecl(self, ctx:FsmParser.ActionDeclContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#funcDecl.
    def visitFuncDecl(self, ctx:FsmParser.FuncDeclContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#machineDecl.
    def visitMachineDecl(self, ctx:FsmParser.MachineDeclContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#interfaceDecl.
    def visitInterfaceDecl(self, ctx:FsmParser.InterfaceDeclContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#instanceDecl.
    def visitInstanceDecl(self, ctx:FsmParser.InstanceDeclContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#inputDecl.
    def visitInputDecl(self, ctx:FsmParser.InputDeclContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#inputRef.
    def visitInputRef(self, ctx:FsmParser.InputRefContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#moveTransition.
    def visitMoveTransition(self, ctx:FsmParser.MoveTransitionContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#selfTransition.
    def visitSelfTransition(self, ctx:FsmParser.SelfTransitionContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#ignoreTransition.
    def visitIgnoreTransition(self, ctx:FsmParser.IgnoreTransitionContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#sources.
    def visitSources(self, ctx:FsmParser.SourcesContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#source.
    def visitSource(self, ctx:FsmParser.SourceContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#triggers.
    def visitTriggers(self, ctx:FsmParser.TriggersContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#trigger.
    def visitTrigger(self, ctx:FsmParser.TriggerContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#pattern.
    def visitPattern(self, ctx:FsmParser.PatternContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#guard.
    def visitGuard(self, ctx:FsmParser.GuardContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#calls.
    def visitCalls(self, ctx:FsmParser.CallsContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#sendCall.
    def visitSendCall(self, ctx:FsmParser.SendCallContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#actionCall.
    def visitActionCall(self, ctx:FsmParser.ActionCallContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#sendTarget.
    def visitSendTarget(self, ctx:FsmParser.SendTargetContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#block.
    def visitBlock(self, ctx:FsmParser.BlockContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#stmt.
    def visitStmt(self, ctx:FsmParser.StmtContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#varStmt.
    def visitVarStmt(self, ctx:FsmParser.VarStmtContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#assignStmt.
    def visitAssignStmt(self, ctx:FsmParser.AssignStmtContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#sayStmt.
    def visitSayStmt(self, ctx:FsmParser.SayStmtContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#ifStmt.
    def visitIfStmt(self, ctx:FsmParser.IfStmtContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#forStmt.
    def visitForStmt(self, ctx:FsmParser.ForStmtContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#returnStmt.
    def visitReturnStmt(self, ctx:FsmParser.ReturnStmtContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#sendStmt.
    def visitSendStmt(self, ctx:FsmParser.SendStmtContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#callStmt.
    def visitCallStmt(self, ctx:FsmParser.CallStmtContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#strExpr.
    def visitStrExpr(self, ctx:FsmParser.StrExprContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#indexExpr.
    def visitIndexExpr(self, ctx:FsmParser.IndexExprContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#binExpr.
    def visitBinExpr(self, ctx:FsmParser.BinExprContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#notExpr.
    def visitNotExpr(self, ctx:FsmParser.NotExprContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#intExpr.
    def visitIntExpr(self, ctx:FsmParser.IntExprContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#arrayExpr.
    def visitArrayExpr(self, ctx:FsmParser.ArrayExprContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#negExpr.
    def visitNegExpr(self, ctx:FsmParser.NegExprContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#refExpr.
    def visitRefExpr(self, ctx:FsmParser.RefExprContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#boolExpr.
    def visitBoolExpr(self, ctx:FsmParser.BoolExprContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#callExpr.
    def visitCallExpr(self, ctx:FsmParser.CallExprContext):
        return self.visitChildren(ctx)


    # Visit a parse tree produced by FsmParser#parenExpr.
    def visitParenExpr(self, ctx:FsmParser.ParenExprContext):
        return self.visitChildren(ctx)



del FsmParser