/* The walk: role mapping + CPG -> ordered interactions. Reasoning in JOERN.md.
 *
 * Language-independent; detector.py sends it followed by one `LangSpec` profile.
 * Read `exec` first: five steps, each a call into a class defined below it.
 *
 * params  roles, behaviour, entry
 * result  {schemaVersion, lang, entryRole, entryRule, anchoredRoles, interactions}
 *         each interaction carries `path`, the calls from the source role's body to the
 *         target, and `parent`, the interaction that entered the source role
 */

import io.shiftleft.codepropertygraph.generated.nodes.{AstNode, Call, CfgNode, ClosureBinding, FieldIdentifier, Identifier, Local, Member, Method, MethodParameterIn, MethodRef, MethodReturn, TypeDecl, TypeRef}

/** What differs between one frontend's CPG and another's, answered by the profile. */
trait LangSpec {

  /** The frontend this profile targets, echoed in the result. */
  def id: String

  /** Whether a citation at line `cited` names a node spanning `line..lineEnd` (-1: no span). */
  def anchorsAt(cited: Int, line: Int, lineEnd: Int): Boolean

  /** Tie-break rank of a node kind (METHOD, TYPE_DECL, PARAM, LOCAL, MEMBER), lowest wins. */
  def declKindRank(kind: String): Int

  /** Whether fullName `member` is declared inside fullName `owner`. */
  def declares(owner: String, member: String): Boolean

  /** Node ids defining `name` as `file` imports it, followed through re-imports. */
  def importedDefinitions(file: String, name: String, root: String): List[Long]

  /** Whether a LOCAL is the binding of an imported name rather than a definition. */
  def isImportBinding(local: Local, root: String): Boolean

  /** Methods bound on entry to `m` that the graph does not record: (target, line, mechanism). */
  def entryEdges(m: Method, root: String): List[(Method, Int, String)]

  /** Method names a framework invokes on an instance it is handed. */
  def invokedOnInstance: List[String]

  /** Repository methods a bare call runs by its file's own import; node ids. */
  def importedCallees(c: Call, root: String): List[Long]

  /** The class whose field `access` reads, when the graph leaves the object untyped. */
  def fieldOwnerType(access: Call, root: String): Option[String]

  /** Bodiless methods the frontend records for `field`'s generated accessors; node ids. */
  def accessorsOf(field: Member, root: String): List[Long]

  /** The inverse of `accessorsOf`: the field a bodiless generated accessor reads or writes. */
  def fieldOfAccessor(accessor: Method, root: String): List[Long]

  /** A module or class body: a `def` there binds a definition, it is not code being run. */
  def isModuleOrClassBody(m: Method): Boolean

  /** Whether a call left on a bodiless method runs the repository's overrides of it. */
  def dispatchesToOverrides: Boolean

  /** Calls a framework passes on to repository code that nothing in the repository calls. */
  def routes: List[Route] = Nil

  /** The annotation marking a method whose return value the framework injects elsewhere, or "". */
  def providerAnnotation: String = ""

  /** The name the frontend gives a constructor call, or "". */
  def constructorName: String = ""
}


// ═══ the run ═════════════════════════════════════════════════════════════════

@main def exec(
  outputPath: String,
  inputPath: String = "",
  language: String = "",
  cpgPath: String = "",
  roles: String = "",
  entry: String = "",
  behaviour: String = ""
) = {
  loadGraph(cpgPath, inputPath, language)
  val repo = new Repo(inputPath)

  // 1. Which method does a call run?
  val graph = new CallGraph(repo)

  // 2. Which node does each citation name, and which role owns which node?
  val located   = new Locator(graph).locateAll(jarray(roles))
  val ownership = new Ownership(graph, located, jarray(behaviour))

  // 3. Where does a run of this code begin?
  val start = Entry.choose(graph, ownership, located, entry)

  // 4. Step the control-flow graph from there, recording each change of role.
  val walker = new Walker(graph, ownership)
  start.node.foreach { case (m, from) =>
    val roles = ownership.roleOfNode.getOrElse(m.id, List(start.role))
    walker.coLocatedAtStart(m, from, roles)
    walker.walkBody(m, from, roles, Nil, 0)
  }

  // 5. Report each interaction once, in the order it was first reached.
  val interactions = firstOccurrences(walker.edges.toList)
  writeResult(outputPath, render(graph, located, start, interactions))
  println(s"derived ${interactions.size} interaction(s) from ${located.size} role(s), entry=${start.role}")
}


// ═══ what the run is made of ═════════════════════════════════════════════════

/** One located node. Only a METHOD has a body, so only a METHOD can be walked. */
case class Decl(id: Long, kind: String, name: String, file: String, line: Int,
                lineEnd: Int = -1)

/** A role and the node(s) its citation located; a library role has `libraryCalls` instead:
  * the repository's calls to it. */
case class RoleNode(role: String, decls: List[Decl], external: Boolean,
                    locatedBy: String = "", libraryCalls: List[Call] = Nil)

/** One call the walk followed: in `caller`, the statement `code` at `file`:`line` ran
  * `callee`, as `mechanism` says; `route` labels a framework hand-off the hop crossed. */
case class Step(caller: String, file: String, line: Int, code: String, callee: String,
                mechanism: String, route: String = "")

/** One interaction: control in `source`'s code reached `target`'s. `path` is every call
  * from the source role's body to the target, the last landing in it; `parent` is the
  * interaction that brought the walk into the source role's body, None at the entry. */
case class Edge(source: String, target: String, file: String, line: Int,
                via: List[String], mechanism: String, path: List[Step] = Nil,
                parent: Option[(String, String)] = None)

/** A call the framework passes on: `method` on `owner` -- or on a receiver one of `getters`
  * returned, when the frontend left the owner unresolved -- runs the first of `steps` that
  * resolves to repository code. `label` marks the framework hop in an edge's `via`. */
case class Route(owner: String, method: String, getters: List[String], label: String,
                 steps: List[RouteStep])

/** What the framework runs: one of `targets` (it picks at runtime), or each in turn (`inOrder`).
  * A target is a method name on any of several types. */
case class RouteStep(inOrder: Boolean, targets: List[(List[String], String)])

/** Methods one hop may run -- alternatives, one of which does -- and how that is known.
  * `provided`: injection providers of the object the call is made on. Their roles are reached
  * at the call itself, but their bodies only build that object, so they are never walked.
  * `bridged` when a framework route supplied the hop; `via` then names the framework step. */
case class Hop(methods: List[Method], mechanism: String, provided: List[Method] = Nil,
               bridged: Boolean = false, via: List[String] = Nil)

/** What a call runs: `hops`, in order. */
case class Resolution(hops: List[Hop]) {
  def all: List[Method] = hops.flatMap(h => h.methods ++ h.provided)
  /** A guess among overrides, as opposed to a literal call or a known framework route. */
  def guessed: Boolean = hops.exists(h => h.mechanism == "dispatch" && !h.bridged)
}


// ── loading, paths, JSON ─────────────────────────────────────────────────────

/** Load the prebuilt CPG, or build one from the source folder. */
def loadGraph(cpgPath: String, inputPath: String, language: String): Unit = {
  if (cpgPath.nonEmpty) {
    if (importCpg(cpgPath, "repo", false).isEmpty)
      throw new RuntimeException(s"could not load the CPG at $cpgPath")
  } else if (language.isEmpty) importCode(inputPath)
  else importCode(inputPath, "repo", language)
  ()
}

/** The repository folder (`inputPath`); turns graph filenames into repo-relative paths. */
class Repo(inputPath: String) {
  val root = if (inputPath.isEmpty) "" else if (inputPath.endsWith("/")) inputPath else inputPath + "/"
  /** Strip the root; Joern's placeholder filenames become "". */
  def rel(p: String): String = {
    val s = if (root.nonEmpty && p.startsWith(root)) p.substring(root.length) else p
    if (s == "<empty>" || s == "N/A" || s == "<unknown>") "" else s
  }
  def norm(p: String): String = p.replace('\\', '/').stripPrefix("./").stripPrefix("/")
  /** The form every file comparison uses. */
  def file(p: String): String = norm(rel(p))
}

/** A JSON value as a string; null and other types become "". */
def jstr(v: ujson.Value): String = v match {
  case ujson.Str(x)  => x
  case ujson.Num(x)  => if (x == x.toLong.toDouble) x.toLong.toString else x.toString
  case _             => ""
}
def jfield(o: ujson.Value, k: String): String =
  scala.util.Try(o.obj.get(k).map(jstr).getOrElse("")).getOrElse("")
def jarray(s: String): List[ujson.Value] =
  ujson.read(if (s.trim.isEmpty) "[]" else s).arr.toList

/** A name the frontend generated (`<module>`, `<init>`, `<lambda>0`, ...): no source
  * identifier contains an angle bracket. */
def isSynthetic(n: String): Boolean = n.contains("<") || n.contains(">")

/** A call the programmer wrote, not a `<operator>` the frontend synthesised. */
def isRealCall(c: Call): Boolean = !c.name.startsWith("<operator>")

/** A call's source text on one line, cut to a readable length. */
def brief(code: String): String = {
  val s = code.replaceAll("\\s+", " ").trim
  if (s.length <= 120) s else s.take(119) + "…"
}


// ── 1. call resolution: which method does a call run? ────────────────────────

/** Questions answered from the graph alone: what a call runs, what a name or
  * field refers to, what a type declares, where a body starts. */
class CallGraph(val repo: Repo) {
  val root = repo.root

  val allMethods = cpg.method.l
  val internal = allMethods.filterNot(_.isExternal).filter(m => repo.rel(m.filename).nonEmpty)
  val methodById: Map[Long, Method] = allMethods.map(m => m.id -> m).toMap
  lazy val memberById: Map[Long, Member] = cpg.member.l.map(m => m.id -> m).toMap

  /** Arguments / parameters, excluding the receiver. */
  def argCount(c: Call): Int = c.argument.count(_.argumentIndex > 0)
  def paramCount(m: Method): Int = m.parameter.count(_.index > 0)

  def hasBody(m: Method): Boolean =
    m.cfgFirst.headOption.exists(n => !n.isInstanceOf[MethodReturn])

  /** Every method a call may run: the name's scope link, else the call's edges,
    * else the import rescue. */
  def calleesOf(c: Call): List[Method] = {
    val all = c.callee.l
    val inRepo = all.filterNot(_.isExternal).distinctBy(_.id)
    val byScope = namedByScope(c)
    if (byScope.nonEmpty) byScope
    else if (inRepo.isEmpty) rescuedCallees(c) match {
      case Nil => all.headOption.toList
      case xs  => xs
    }
    else if (inRepo.sizeIs == 1) inRepo
    else {
      val addressed = inRepo.filter(_.fullName == c.methodFullName).take(1)
      if (addressed.exists(hasBody)) addressed
      else (addressed ++ inRepo.filter(hasBody).sortBy(_.fullName)) match {
        case Nil => inRepo.sortBy(_.fullName).take(1)
        case xs  => xs
      }
    }
  }

  /** Upper bound on the overrides one call fans out to; beyond it, none are followed. */
  val MAX_OVERRIDES = 32

  /** What a call runs and how that is known, first answer wins: its callees (`call`);
    * when none has a body, the repository's overrides of it on the receiver's declared
    * type (`dispatch`); then a framework route the profile declares (`dispatch`,
    * `bridged`); then the repository's injection providers of the receiver's type
    * (`dependency-injection`). A route or a provider only fills a gap the code leaves,
    * and is added after what the call resolved to without it, which it never replaces. */
  def targetsOf(c: Call): Resolution = {
    val direct = calleesOf(c)
    val plain = Resolution(List(Hop(direct, "call")))
    if (direct.exists(m => !m.isExternal && hasBody(m))) plain
    else {
      val overrides = if (Lang.dispatchesToOverrides) overridesOf(c) else Nil
      if (overrides.nonEmpty) Resolution(List(Hop(overrides, "dispatch")))
      else routed(c) match {
        case Some(r) => Resolution(plain.hops ++ r.hops)
        case None    => injected(c) match {
          case Nil => plain
          case xs  => Resolution(List(Hop(direct, "call", provided = xs)))
        }
      }
    }
  }

  /** Repository methods overriding `c`'s target: same name and arity, with a body,
    * declared in a subtype (at any depth) of the receiver's declared type. */
  def overridesOf(c: Call): List[Method] = implementationsOf(declaredOwner(c), c.name, argCount(c))

  /** Repository methods named `name` with a body, declared in a subtype (at any depth)
    * of `owner`; `arity` < 0 matches any. */
  def implementationsOf(owner: String, name: String, arity: Int = -1): List[Method] = {
    val found = subtypesOf(owner)
      .flatMap(t => methodsOfType.getOrElse(t.id, Nil)).distinct.flatMap(methodById.get)
      .filter(m => m.name == name && !m.isExternal && hasBody(m) && (arity < 0 || paramCount(m) == arity))
      .distinctBy(_.id).sortBy(_.fullName)
    if (found.sizeIs > MAX_OVERRIDES) Nil else found
  }

  /** The first declared route `c` matches, resolved to its first step that reaches repository
    * code. A repository implementation of the route's own type is ordinary dispatch instead. */
  def routed(c: Call): Option[Resolution] =
    Lang.routes.find(r => routeMatches(r, c)).flatMap { r =>
      implementationsOf(r.owner, r.method, argCount(c)) match {
        case own if own.nonEmpty => Some(Resolution(List(Hop(own, "dispatch"))))
        case _ =>
          r.steps.iterator.map { s =>
            val hops = s.targets.flatMap { case (types, name) => hopTo(types, name) }
            if (s.inOrder) hops else hops.take(1)
          }.find(_.nonEmpty).map(hops => Resolution(hops.map(_.copy(bridged = true, via = List(r.label)))))
      }
    }

  /** The repository code a framework call on `types`.`name` lands in: an implementation, else
    * an injection provider of one of the types. */
  private def hopTo(types: List[String], name: String): Option[Hop] = {
    val impls = types.flatMap(t => implementationsOf(t, name)).distinctBy(_.id)
    lazy val provided = types.flatMap(providersOf).distinctBy(_.id)
    if (impls.nonEmpty) Some(Hop(impls, "dispatch"))
    else if (provided.nonEmpty) Some(Hop(Nil, "dispatch", provided = provided))
    else None
  }

  /** A route applies to a call of its method on its owner -- or, when the frontend could not
    * type the receiver, on a receiver one of its getters returned. */
  def routeMatches(r: Route, c: Call): Boolean = c.name == r.method && {
    val owner = declaredOwner(c)
    owner == r.owner || (owner.nonEmpty && simpleName(owner) == simpleName(r.owner)) ||
      c.argument.l.find(_.argumentIndex == 0).exists {
        case g: Call => r.getters.contains(g.name)
        case _       => false
      }
  }

  /** For a call on a library type nothing in the repository implements: the repository's
    * providers of that type, which is what the framework injects into the receiver. */
  def injected(c: Call): List[Method] = providersOf(declaredOwner(c))

  /** Repository methods carrying the profile's provider annotation. */
  lazy val providers: List[Method] =
    if (Lang.providerAnnotation.isEmpty) Nil
    else internal.filter(m => m.annotation.name.l.contains(Lang.providerAnnotation))
  lazy val providerIds: Set[Long] = providers.map(_.id).toSet

  /** Providers whose declared return type is `t`, by fullName or, unresolved, simple name. */
  def providersOf(t: String): List[Method] =
    if (t.isEmpty || t == "ANY") Nil
    else providers.filter { m =>
      val r = m.methodReturn.typeFullName
      r == t || simpleName(r) == simpleName(t)
    }.sortBy(_.fullName)

  /** The type a call is addressed to: its fullName's owner, else the receiver's type. */
  def declaredOwner(c: Call): String = {
    val cut = c.methodFullName.indexOf("." + c.name + ":")
    val owner = if (cut > 0) c.methodFullName.substring(0, cut) else ""
    if (owner.nonEmpty && !owner.contains("<")) owner
    else c.argument.l.find(_.argumentIndex == 0).map(typeOf).getOrElse("")
  }

  private def simpleName(t: String): String = t.split('.').lastOption.getOrElse(t)

  /** Repository classes by each parent they name, as written and by simple name. */
  private lazy val childrenOf: Map[String, List[TypeDecl]] =
    typeDeclsByName.values.flatten.toList
      .flatMap(t => t.inheritsFromTypeFullName.l.map(_ -> t)).groupBy(_._1)
      .map { case (k, vs) => k -> vs.map(_._2).distinctBy(_.id) }
  private lazy val childrenBySimpleName =
    childrenOf.toList.groupBy { case (k, _) => simpleName(k) }
      .map { case (k, vs) => k -> vs.flatMap(_._2).distinctBy(_.id) }

  /** Every repository subtype of `owner`; an unresolved or library parent is also
    * matched by simple name, since the frontend may record it either way. */
  def subtypesOf(owner: String): List[TypeDecl] = {
    if (owner.isEmpty) return Nil
    val first = childrenOf.getOrElse(owner, Nil) match {
      case Nil if !typeDeclsByName.contains(owner) => childrenBySimpleName.getOrElse(simpleName(owner), Nil)
      case xs  => xs
    }
    val seen = scala.collection.mutable.LinkedHashMap[Long, TypeDecl]()
    var level = first
    while (level.nonEmpty) {
      val fresh = level.filterNot(t => seen.contains(t.id))
      fresh.foreach(t => seen(t.id) = t)
      level = fresh.flatMap(t => childrenOf.getOrElse(t.fullName, Nil))
    }
    seen.values.toList
  }

  /** The repository methods a stub-only call may run, as the language resolves it;
    * arity narrows them when it leaves any -- keyword and default arguments make it a hint. */
  def rescuedCallees(c: Call): List[Method] = {
    val declared = Lang.importedCallees(c, root).distinct.flatMap(methodById.get).filterNot(_.isExternal)
    val byArity = declared.filter(paramCount(_) == argCount(c))
    if (declared.sizeIs <= 1 || byArity.isEmpty) declared else byArity
  }

  /** closureBindingId -> the variable a captured name refers to, one scope out. */
  lazy val capturedFrom: Map[String, AstNode] =
    cpg.all.collectAll[ClosureBinding].l.flatMap { cb =>
      cb.closureBindingId.flatMap(id => cb._refOut.collectFirst { case n: AstNode => n }.map(id -> _))
    }.toMap

  /** The variable a name use refers to: its REF, then out through closure captures. */
  def declarationOf(i: Identifier): Option[AstNode] = {
    var cur: Option[AstNode] = i._refOut.collectFirst { case n: AstNode => n }
    var hops = 0
    def captured(n: Option[AstNode]) = n.collect { case l: Local => l }.flatMap(_.closureBindingId).flatMap(capturedFrom.get)
    while (hops < 32 && captured(cur).nonEmpty) { cur = captured(cur); hops += 1 }
    cur
  }

  /** Node ids a name use stands for; an import is followed to its definition. */
  def definitionsBehind(i: Identifier): List[Long] = declarationOf(i).toList.flatMap {
    case l: Local if Lang.isImportBinding(l, root) =>
      Lang.importedDefinitions(repo.file(l.method.filename.headOption.getOrElse("")), l.name, root)
    case n => List(n.id)
  }

  /** For a call through a plain name (`f()`), the repository methods that name means by scope. */
  def namedByScope(c: Call): List[Method] = {
    val methods = c.receiver.collectFirst { case i: Identifier if i.name == c.name => i }.toList
      .flatMap(definitionsBehind).distinct.flatMap(methodById.get).filterNot(_.isExternal)
    if (methods.sizeIs <= 1) methods else methods.filter(paramCount(_) == argCount(c))
  }

  /** The field a field access reads or writes: the object's class, else its nearest parent. */
  def fieldsOf(access: Call): List[Member] = {
    val field = access.argument.collectFirst { case f: FieldIdentifier => f.canonicalName }
    val objectType = access.argument.l.find(_.argumentIndex == 1).map(typeOf)
      .filter(typeDeclsByName.contains)
      .orElse(Lang.fieldOwnerType(access, root))
    (for (f <- field; t <- objectType) yield fieldIn(t, f)).getOrElse(Nil)
  }

  private def typeOf(n: AstNode): String = n match {
    case i: Identifier => i.typeFullName
    case c: Call       => c.typeFullName
    case t: TypeRef    => t.typeFullName
    case _             => ""
  }

  /** Repository classes by fullName, from real source files only. */
  lazy val typeDeclsByName =
    cpg.typeDecl.filterNot(_.isExternal).l.filter(t => repo.rel(t.filename).nonEmpty).groupBy(_.fullName)

  /** Members named `field` of `typeName`, else of its parents; an ambiguous parent level gives none. */
  private def fieldIn(typeName: String, field: String): List[Member] = {
    val seen = scala.collection.mutable.Set[String]()
    var level = List(typeName)
    var found = List.empty[Member]
    var first = true
    while (level.nonEmpty && found.isEmpty) {
      val decls = level.filter(seen.add).flatMap(t => typeDeclsByName.getOrElse(t, Nil))
      found = decls.flatMap(_.member.nameExact(field).l).distinctBy(_.id)
      if (!first && found.sizeIs > 1) found = Nil
      level = if (found.isEmpty) decls.flatMap(_.inheritsFromTypeFullName).distinct else Nil
      first = false
    }
    found
  }

  /** Repository methods passed to a call as arguments (METHOD_REF only). */
  def methodsPassedTo(c: Call): List[Method] =
    c.argument.ast.l.collect { case mr: MethodRef => mr }
      .flatMap(mr => scala.util.Try(Option(mr.referencedMethod)).getOrElse(None))
      .filterNot(_.isExternal).distinct

  /** The method a METHOD_REF refers to, if it is in this repository. */
  def referencedMethodInRepo(mr: MethodRef): Option[Method] =
    scala.util.Try(Option(mr.referencedMethod)).getOrElse(None).filterNot(_.isExternal)

  /** Inside a real call's arguments -- `methodsPassedTo` enters it at that call. */
  def isCallArgument(mr: MethodRef): Boolean =
    Iterator.iterate[AstNode](mr)(_.astParent).drop(1)
      .takeWhile(p => p != null && !p.isInstanceOf[Method])
      .exists { case c: Call => isRealCall(c); case _ => false }

  /** TYPE_DECL id -> the non-synthetic methods it declares. */
  lazy val methodsOfType: Map[Long, List[Long]] =
    cpg.typeDecl.filterNot(_.isExternal).l.map { td =>
      td.id -> allMethods
        .filter(m => Lang.declares(td.fullName, m.fullName) && !isSynthetic(m.name))
        .map(_.id)
    }.toMap

  /** The bodies a node stands for: a method itself, or a class's methods. */
  def bodiesOf(e: Decl): List[Method] = e.kind match {
    case "METHOD"    => methodById.get(e.id).toList
    case "TYPE_DECL" => methodsOfType.getOrElse(e.id, Nil).flatMap(methodById.get)
    case _           => Nil
  }

  /** Where a role's walk begins: a declaration's body from its first node, or a
    * variable's enclosing body from the cited line. */
  def startsOf(d: Decl): List[(Method, Option[CfgNode])] = d.kind match {
    case "METHOD" | "TYPE_DECL" => bodiesOf(d).map(m => (m, None))
    case "LOCAL" | "MEMBER" =>
      enclosingMethodOf(d).toList.flatMap(m => firstCfgNodeAt(m, d.line).map(n => (m, Some(n))))
    case _ => Nil
  }

  /** The first node on `line` in execution order, or None if control never reaches it. */
  def firstCfgNodeAt(m: Method, line: Int): Option[CfgNode] = {
    val seen = scala.collection.mutable.Set[Long]()
    val pending = scala.collection.mutable.Stack[CfgNode]()
    m.cfgFirst.headOption.foreach(pending.push)
    var found: Option[CfgNode] = None
    while (pending.nonEmpty && found.isEmpty) {
      val n = pending.pop()
      if (seen.add(n.id)) {
        if (n.lineNumber.contains(line) && !n.isInstanceOf[MethodReturn]) found = Some(n)
        else n._cfgOut.collect { case c: CfgNode => c }.toList.reverse.foreach(pending.push)
      }
    }
    found
  }

  def enclosingMethodOf(d: Decl): Option[Method] = enclosingMethodAt(d.file, d.line)

  /** The smallest method whose line span contains `line` of `file`. */
  def enclosingMethodAt(file: String, line: Int): Option[Method] =
    internal.filter(m => repo.file(m.filename) == file &&
      m.lineNumber.exists(_ <= line) && m.lineNumberEnd.exists(_ >= line))
      .sortBy(m => m.lineNumberEnd.getOrElse(0) - m.lineNumber.getOrElse(0)).headOption
}


// ── 2. locate each role's entity in the graph ────────────────────────────────

/** Finds the node each citation (entity, file, line) names. */
class Locator(graph: CallGraph) {
  private val repo = graph.repo

  /** Every node a citation could name: methods, types, locals, parameters, members. */
  val declarations: List[Decl] = {
    val ms = graph.internal.map(m =>
      Decl(m.id, "METHOD", m.name, repo.file(m.filename), m.lineNumber.getOrElse(-1),
          m.lineNumberEnd.getOrElse(-1)))
    val ts = cpg.typeDecl.filterNot(_.isExternal).l.map(t =>
      Decl(t.id, "TYPE_DECL", t.name, repo.file(t.filename), t.lineNumber.getOrElse(-1)))
    val ls = cpg.local.l.map(l =>
      Decl(l.id, "LOCAL", l.name, repo.file(l.method.filename.headOption.getOrElse("")),
          l.lineNumber.getOrElse(-1)))
    val bs = cpg.member.l.map(b =>
      Decl(b.id, "MEMBER", b.name, repo.file(b.typeDecl.filename), b.lineNumber.getOrElse(-1)))
    val ps = cpg.parameter.l.filter(_.name.nonEmpty).map(p =>
      Decl(p.id, "PARAM", p.name, repo.file(p.method.filename), p.lineNumber.getOrElse(-1)))
    ms ++ ts ++ ls ++ ps ++ bs
  }

  val declById: Map[Long, Decl] = declarations.map(e => e.id -> e).toMap

  lazy val localById: Map[Long, Local] = cpg.local.l.map(l => l.id -> l).toMap

  /** Whether a declaration is only an import of the name. */
  def isImportBinding(d: Decl): Boolean =
    d.kind == "LOCAL" && localById.get(d.id).exists(Lang.isImportBinding(_, graph.root))

  def preference(e: Decl): Int = Lang.declKindRank(e.kind)

  def locateAll(rows: List[ujson.Value]): List[RoleNode] =
    rows.map(locate).filter(_.role.nonEmpty)

  /** One citation's node, by the first reading that finds one: declaration on the
    * line, use on the line, the file's import, then same-named declarations in the file. */
  def locate(r: ujson.Value): RoleNode = {
    val role   = jfield(r, "role")
    val entity = jfield(r, "entity")
    val file   = repo.norm(jfield(r, "file"))
    val line   = citedLine(jfield(r, "lines"))
    val bare   = entity.split("\\.").lastOption.getOrElse(entity)

    val named =
      if (bare.trim.isEmpty || isSynthetic(bare)) Nil
      else declarations.filter(e => e.name == bare && !isSynthetic(e.name))
    val pool = (if (file.isEmpty) named else named.filter(_.file == file)).filterNot(isImportBinding)

    val ranked = pool.sortBy(preference)
    val best = if (ranked.isEmpty) Nil else ranked.filter(e => preference(e) == preference(ranked.head))
    val declsAtLine = if (line < 0) Nil else ranked.filter(_.line == line)
    val occ = occurrencesAt(bare, file, line).distinct.sortBy(preference)
    val occAbove =
      if (declsAtLine.nonEmpty || occ.nonEmpty) Nil
      else occurrencesAbove(bare, file, line).distinct.sortBy(preference)
    val viaImport =
      if (file.isEmpty || declsAtLine.nonEmpty || occ.nonEmpty || occAbove.nonEmpty || bare.trim.isEmpty) Nil
      else importedDefinition(file, bare).filterNot(d => pool.exists(_.id == d.id))

    val locatedBy =
      if (declsAtLine.nonEmpty) "declaration-at-line"
      else if (occ.nonEmpty) "occurrence-at-line"
      else if (occAbove.nonEmpty) "occurrence-above-line"
      else if (viaImport.nonEmpty) "import"
      else if (best.isEmpty) ""
      else if (best.sizeIs == 1) "unique-name"
      else if (line < 0) "first-of-many"
      else "line-tiebreak"
    val picked =
      if (declsAtLine.nonEmpty) declsAtLine.take(1)
      else if (occ.nonEmpty) occ.take(1)
      else if (occAbove.nonEmpty) occAbove.take(1)
      else if (viaImport.nonEmpty) viaImport
      else if (best.isEmpty) Nil
      else if (best.sizeIs == 1) best
      else if (line < 0) best.take(1)
      else best.filter(e => Lang.anchorsAt(line, e.line, e.lineEnd)).take(1)

    if (picked.nonEmpty) RoleNode(role, picked, external = false, locatedBy)
    else {
      val (calls, by) = locateLibraryCalls(bare, file, line)
      RoleNode(role, Nil, external = true, by, calls)
    }
  }

  /** For a role with no repository node -- a library -- the repository's calls to `name` its
    * citation names: on the cited line, else in the method containing that line, else the
    * nearest ones above the line (`nearestCallsAbove`). */
  def locateLibraryCalls(name: String, file: String, line: Int): (List[Call], String) =
    if (name.trim.isEmpty || isSynthetic(name) || file.isEmpty || line < 0) (Nil, "")
    else {
      // A class named as a library role is used by constructing it: `new X()` is a call to the
      // profile's constructor name, owned by X.
      val ctors =
        if (Lang.constructorName.isEmpty) Nil
        else cpg.call.nameExact(Lang.constructorName).l.filter { c =>
          val cut = c.methodFullName.indexOf("." + Lang.constructorName)
          cut > 0 && c.methodFullName.substring(0, cut).split("[.$]").lastOption.contains(name)
        }
      val calls = (cpg.call.nameExact(name).l ++ ctors)
        .filter(c => isRealCall(c) && repo.file(c.method.filename) == file)
      val onLine = calls.filter(_.lineNumber.contains(line))
      lazy val inMethod = graph.enclosingMethodAt(file, line).toList
        .flatMap(m => calls.filter(_.method.id == m.id))
      lazy val above = nearestCallsAbove(calls, file, line)
      if (onLine.nonEmpty) (onLine, "call-at-line")
      else if (inMethod.nonEmpty) (inMethod, "call-in-method")
      else if (above.nonEmpty) (above, "call-above-line")
      else (Nil, "")
    }

  /** Of `calls`, those a citation on `line` names when none is recorded on it. The frontend
    * records a call on the line its expression starts on, so `.name(...)` written further down
    * a chain -- or in a lambda nested in one -- sits above the line it is written on. Walks up
    * from `line` to the first line holding one, within the named method around `line`. */
  def nearestCallsAbove(calls: List[Call], file: String, line: Int): List[Call] =
    graph.internal
      .filter(m => repo.file(m.filename) == file && !isSynthetic(m.name) &&
        m.lineNumber.exists(_ <= line) && m.lineNumberEnd.exists(_ >= line))
      .sortBy(m => m.lineNumberEnd.getOrElse(0) - m.lineNumber.getOrElse(0)).headOption.toList
      .flatMap { m =>
        val above = calls.filter(_.lineNumber.exists(l => l < line && m.lineNumber.exists(_ <= l)))
        above.flatMap(_.lineNumber).maxOption.toList.flatMap(l => above.filter(_.lineNumber.contains(l)))
      }

  /** The repository node a call to `name` names, for a citation on `line` with nothing named
    * `name` recorded on it (`nearestCallsAbove`). */
  def occurrencesAbove(name: String, file: String, line: Int): List[Decl] =
    if (name.trim.isEmpty || isSynthetic(name) || file.isEmpty || line < 0) Nil
    else nearestCallsAbove(
      cpg.call.nameExact(name).l.filter(c => isRealCall(c) && repo.file(c.method.filename) == file),
      file, line
    ).flatMap(calledDecl).distinct.flatMap(declById.get).filterNot(isImportBinding)

  /** The node id of the method a call runs, when that is one method -- or, when it is a
    * generated accessor with no source of its own, of the field behind it (`fieldOfAccessor`),
    * which `Ownership` then hands every call to that accessor. */
  private def calledDecl(c: Call): List[Long] = {
    val cs = graph.calleesOf(c)
    cs.find(_.fullName == c.methodFullName).orElse(if (cs.sizeIs == 1) cs.headOption else None).toList
      .flatMap(m => if (m.isExternal) Lang.fieldOfAccessor(m, graph.root) else List(m.id))
  }

  /** The first line of a "12-20" range (any dash: the portal writes "12–20"), or -1. */
  def citedLine(s: String): Int = {
    val head = s.split("[-‒–—−]").headOption.getOrElse("").trim
    scala.util.Try(head.toInt).getOrElse(-1)
  }

  /** What a use of `name` on `line` of `file` refers to: a variable, a call's
    * method, or a field -- possibly declared in another file. */
  def occurrencesAt(name: String, file: String, line: Int): List[Decl] =
    if (name.trim.isEmpty || line < 0) Nil
    else {
      val fromUses = cpg.identifier.nameExact(name).l
        .filter(i => repo.file(i.method.filename) == file)
        .filter(_.lineNumber.contains(line))
        .flatMap(graph.definitionsBehind)
      val fromCalls = cpg.call.nameExact(name).l
        .filter(c => repo.file(c.method.filename) == file)
        .filter(_.lineNumber.contains(line))
        .flatMap(calledDecl)
      val fromFields = cpg.fieldIdentifier.canonicalNameExact(name).l
        .filter(fi => repo.file(fi.method.filename) == file && fi.lineNumber.contains(line))
        .flatMap(_.astParent match { case c: Call => graph.fieldsOf(c).map(_.id); case _ => Nil })
      (fromUses ++ fromCalls ++ fromFields).distinct.flatMap(declById.get).filterNot(isImportBinding)
    }

  /** Where `name`, as `file` imports it, is defined; only a single best candidate. */
  def importedDefinition(file: String, name: String): List[Decl] = {
    val found = Lang.importedDefinitions(file, name, graph.root).distinct
      .flatMap(declById.get).filterNot(isImportBinding).sortBy(preference)
    val top = found.filter(e => found.headOption.exists(h => preference(e) == preference(h)))
    if (top.sizeIs == 1) top else Nil
  }
}


// ── which role owns which node ───────────────────────────────────────────────

/** Lookups the walk asks at every node: whose code is this, and in what order
  * do roles sharing a node speak. */
class Ownership(graph: CallGraph, located: List[RoleNode], behaviourSteps: List[ujson.Value]) {

  /** Node id -> every role located on it; a cited class also owns its methods, a
    * cited field its generated accessors, and a library role its call sites. */
  val roleOfNode: Map[Long, List[String]] = {
    val direct    = located.flatMap(a => a.decls.map(_.id -> a.role))
    val directIds = direct.map(_._1).toSet
    val viaAccessor = located.flatMap { a =>
      a.decls.filter(_.kind == "MEMBER").flatMap(e => graph.memberById.get(e.id)).flatMap { f =>
        Lang.accessorsOf(f, graph.root).filterNot(directIds.contains).map(_ -> a.role)
      }
    }
    val accessorIds = viaAccessor.map(_._1).toSet
    val viaType = located.flatMap { a =>
      a.decls.filter(_.kind == "TYPE_DECL").flatMap { e =>
        graph.methodsOfType.getOrElse(e.id, Nil)
          .filterNot(id => directIds.contains(id) || accessorIds.contains(id)).map(_ -> a.role)
      }
    }
    val atLibraryCalls = located.flatMap(a => a.libraryCalls.map(_.id -> a.role))
    // A library role cited where an injection provider builds it (`return new BCryptPasswordEncoder()`)
    // is what that provider hands out, so a call reaching the provider reaches the role.
    val viaProvider = located.flatMap { a =>
      a.libraryCalls.map(_.method.id).filter(graph.providerIds.contains).distinct
        .filterNot(directIds.contains).map(_ -> a.role)
    }
    (direct ++ viaAccessor ++ viaType ++ atLibraryCalls ++ viaProvider).groupBy(_._1).map { case (k, vs) => k -> vs.map(_._2).distinct }
  }

  /** Role -> the earliest pattern step it appears in; orders, never filters. */
  val appearance: Map[String, Int] = {
    val firstStep = scala.collection.mutable.LinkedHashMap[String, Int]()
    behaviourSteps.zipWithIndex.foreach { case (b, i) =>
      val at = scala.util.Try(jfield(b, "step").trim.toInt).toOption.getOrElse(i)
      List(jfield(b, "from"), jfield(b, "to")).filter(_.nonEmpty).foreach { r =>
        if (!firstStep.contains(r)) firstStep(r) = at
      }
    }
    firstStep.toMap
  }

  def appearanceOf(role: String): Int = appearance.getOrElse(role, Int.MaxValue)

  /** Every (source, target) pair across two role sets, self-pairs dropped, in pattern order. */
  def pairsFor(sources: List[String], targets: List[String]): List[(String, String)] =
    inPatternOrder(for (s <- sources; t <- targets if s != t) yield (s, t))

  /** Pairs by the target's first appearance in the pattern, then the source's. */
  def inPatternOrder(pairs: List[(String, String)]): List[(String, String)] =
    if (pairs.sizeIs <= 1) pairs
    else pairs.sortBy { case (s, t) => (appearanceOf(t), appearanceOf(s)) }

  /** (from, to) of every step in the pattern's walkthrough. */
  val prescribed: Set[(String, String)] =
    behaviourSteps.map(b => (jfield(b, "from"), jfield(b, "to")))
      .filter { case (f, t) => f.nonEmpty && t.nonEmpty && f != t }.toSet

  /** Library roles cited on one call. What passes between them happens inside the library,
    * where nothing shows which part plays which role, so no interaction is read between them. */
  val sameLibraryCall: Set[(String, String)] =
    located.filter(_.external).flatMap(a => a.libraryCalls.map(_.id -> a.role))
      .groupBy(_._1).values.map(_.map(_._2).distinct)
      .flatMap(rs => for (a <- rs; b <- rs if a != b) yield (a, b)).toSet

  /** Among roles located on one node, the pairs the pattern prescribes. The code holds no
    * separate site between them -- the citation says one entity plays every one of them -- so
    * their interactions are the pattern's, read at that node. Not on a library call
    * (`sameLibraryCall`): there the entity is not the repository's to read. */
  def prescribedAmong(roles: List[String]): List[(String, String)] =
    (for (a <- roles; b <- roles if a != b && prescribed((a, b)) && !sameLibraryCall((a, b))) yield (a, b)).distinct
}


// ── 3. entry role ────────────────────────────────────────────────────────────

/** The role the walk starts from, the rule that chose it (`behaviour`,
  * `code-root`, or "" for none), and its start nodes -- every body a cited class
  * declares, the same set `reachesFrom` judged it a root on. */
case class Entry(role: String, rule: String, node: List[(Method, Option[CfgNode])])

object Entry {

  /** Among roots (roles no other role reaches): the requested one, else the one
    * reaching the most roles. No root, no entry. */
  def choose(graph: CallGraph, ownership: Ownership, located: List[RoleNode],
             requested: String): Entry = {
    val withBody = located.filter(a => a.decls.flatMap(graph.startsOf).nonEmpty)
    val reached = reachesFrom(graph, ownership, withBody)
    val reachedByOthers = reached.values.flatten.toSet
    val roots = withBody.map(_.role).filterNot(reachedByOthers.contains)

    val (role, rule) =
      if (requested.nonEmpty && roots.contains(requested)) (requested, "behaviour")
      else if (roots.nonEmpty)
        (roots.sortBy(r => (-reached.getOrElse(r, Set.empty).size, ownership.appearanceOf(r))).head,
         "code-root")
      else ("", "")

    val node = withBody.find(_.role == role).toList.flatMap(_.decls.flatMap(graph.startsOf))
    Entry(role, rule, node)
  }

  /** Role -> the roles its own bodies reach directly, by call or entry binding. */
  def reachesFrom(graph: CallGraph, ownership: Ownership,
                  withBody: List[RoleNode]): Map[String, Set[String]] =
    withBody.map { a =>
      a.role -> a.decls.flatMap(graph.startsOf).flatMap { case (m, from) =>
        val calls = m.call.filter(isRealCall).l
          .flatMap(graph.targetsOf(_).all).flatMap(t => ownership.roleOfNode.getOrElse(t.id, Nil))
        val bound =
          if (from.nonEmpty) Nil
          else Lang.entryEdges(m, graph.root).flatMap { case (t, _, _) => ownership.roleOfNode.getOrElse(t.id, Nil) }
        (calls ++ bound).filterNot(_ == a.role)
      }.toSet
    }.toMap
}


// ── 4. the walk: step the control-flow graph ─────────────────────────────────

/** Steps method bodies node by node from the entry, recording an edge whenever
  * control reaches another role. */
class Walker(graph: CallGraph, ownership: Ownership) {
  import ownership.{roleOfNode, pairsFor, prescribedAmong, inPatternOrder}

  /** JVM-stack guard, not a search limit. */
  val MAX_DEPTH = 512

  val edges = scala.collection.mutable.ListBuffer[Edge]()
  /** (method, start node, roles) already walked. */
  val enteredBodies = scala.collection.mutable.Set[(Long, Long, String)]()

  /** Walk each alternative in isolation and keep the one reaching the most distinct
    * roles, then the one through the fewest helpers, then the earlier; the rest
    * leave no trace. */
  def keepMostRoles(alternatives: List[() => Unit]): Unit = {
    val (mark, entered) = (edges.size, enteredBodies.clone())
    var best: Option[((Int, Int), List[Edge], scala.collection.mutable.Set[(Long, Long, String)])] = None
    alternatives.foreach { walk =>
      walk()
      val added = edges.drop(mark).toList
      val rank = (added.map(_.target).distinct.size, -added.map(_.via.size).sum)
      if (rank._1 > 0 && best.forall(b => Ordering[(Int, Int)].lt(b._1, rank)))
        best = Some((rank, added, enteredBodies.clone()))
      edges.remove(mark, edges.size - mark)
      enteredBodies.clear(); enteredBodies ++= entered
    }
    best.foreach { case (_, added, after) =>
      edges ++= added
      enteredBodies.clear(); enteredBodies ++= after
    }
  }

  /** The start node is reached by no call, so the pairs the pattern prescribes among the
    * roles sharing it are emitted here, at the start, before its body runs. */
  def coLocatedAtStart(m: Method, from: Option[CfgNode], roles: List[String]): Unit = {
    val line = from.flatMap(_.lineNumber).orElse(m.lineNumber).getOrElse(-1)
    ownership.inPatternOrder(ownership.prescribedAmong(roles)).foreach { case (s, t) =>
      edges += Edge(s, t, graph.repo.file(m.filename), line, Nil, "co-located")
    }
  }

  /** Execute `m` as `sources`, from `from` or its first node; `via` is the helper
    * trail, `dispatched` says that trail entered a `dispatch` override, and `framework`
    * that it crossed a framework route. `path` is the calls since the last role, and
    * `enteredBy` the interactions whose target body this is. */
  def walkBody(m: Method, from: Option[CfgNode], sources: List[String],
               via: List[String], depth: Int, dispatched: Boolean = false,
               framework: Boolean = false, path: List[Step] = Nil,
               enteredBy: List[(String, String)] = Nil): Unit = {
    val key = (m.id, from.map(_.id).getOrElse(-1L), sources.sorted.mkString(" "))
    if (depth > MAX_DEPTH || !enteredBodies.add(key)) return
    val file = graph.repo.file(m.filename)

    /** Control arrived at a target (through call `site`, if any): emit an edge if
      * it is another role's and continue inside it; step into a non-role helper as the same role.
      * An edge reached through an override or a framework route is itself `dispatch`, whatever
      * its last hop was. A route is a known framework rule, not a guess, so it does not use up
      * the trail's one override hop. The roles of `provided` injection providers are reached at
      * this same call -- ordered with the rest by the pattern, not after them -- and never walked.
      * Roles sharing the node reached also get the pairs among them the pattern prescribes
      * (`co-located`): one entity cited for several roles plays each of them there.
      * `name` names a target that is not a method (a field, a variable). */
    def emitAndDescend(targetId: Option[Long], target: Option[Method], site: Option[Call],
                       line: Int, mechanism: String, intoHelper: Boolean = true,
                       bridged: Boolean = false, hopVia: List[String] = Nil,
                       provided: List[Method] = Nil, name: String = ""): Unit = {
      val reached = (targetId.toList ++ site.map(_.id)).flatMap(id => roleOfNode.getOrElse(id, Nil)).distinct
      val injectedRoles = provided.flatMap(p => roleOfNode.getOrElse(p.id, Nil)).distinct.filterNot(reached.contains)
      val pairs = pairsFor(sources, reached ++ injectedRoles)
      val inferred = dispatched || (mechanism == "dispatch" && !bridged)
      val routed = framework || bridged
      val trail = via ++ hopVia
      val tag = if ((dispatched || routed) && mechanism == "call") "dispatch" else mechanism
      val among = if (pairs.isEmpty) Nil else prescribedAmong(reached ++ injectedRoles).filterNot(pairs.contains)

      // This hop, as the step a path records: the call made here and what it ran.
      val code = site.map(c => brief(c.code)).getOrElse("")
      val callee = target.map(_.fullName).orElse(site.map(_.methodFullName)).getOrElse(name)
      def step(to: String, how: String) = Step(m.fullName, file, line, code, to, how, hopVia.mkString(" "))
      def stepInto(role: String) =
        if (!injectedRoles.contains(role)) step(callee, mechanism)
        else step(provided.find(p => roleOfNode.getOrElse(p.id, Nil).contains(role)).map(_.fullName)
                    .getOrElse(callee), "dependency-injection")

      inPatternOrder(pairs ++ among).foreach { case (s, t) =>
        val how = if (among.contains((s, t))) "co-located"
                  else if (injectedRoles.contains(t)) "dependency-injection" else tag
        // A co-located pair's source was reached at this same call, by the pair targeting it.
        val parent = if (among.contains((s, t))) pairs.find(_._2 == s) else enteredBy.find(_._2 == s)
        edges += Edge(s, t, file, line, trail, how, path :+ stepInto(t), parent)
      }
      val entered = pairs.map(_._2).filterNot(injectedRoles.contains).distinct
      if (entered.nonEmpty)
        target.filterNot(_.isExternal).foreach(t =>
          walkBody(t, None, entered, Nil, depth + 1, enteredBy = pairs.filter(p => entered.contains(p._2))))
      else if (reached.isEmpty && intoHelper)
        target.filterNot(_.isExternal)
          .foreach(t => walkBody(t, None, sources, trail :+ t.fullName, depth + 1, inferred, routed,
                                 path :+ step(t.fullName, mechanism), enteredBy))
    }

    if (from.isEmpty) Lang.entryEdges(m, graph.root).foreach { case (target, line, mechanism) =>
      emitAndDescend(Some(target.id), Some(target), None, line, mechanism)
    }

    val visitedNodes = scala.collection.mutable.Set[Long]()
    val pending = scala.collection.mutable.Stack[CfgNode]()
    from.orElse(m.cfgFirst.headOption).foreach(pending.push)

    while (pending.nonEmpty) {
      val n = pending.pop()
      if (visitedNodes.add(n.id)) {
        val line = n.lineNumber.getOrElse(-1)

        n match {
          case c: Call if isRealCall(c) =>
            graph.methodsPassedTo(c).foreach { t =>
              emitAndDescend(Some(t.id), Some(t), None, line, "route-registration")
            }
            // One override hop per helper trail: an override's own delegate calls
            // would fan out to every override again, guessing on top of a guess.
            val resolved = graph.targetsOf(c) match {
              case r if r.guessed && dispatched => Resolution(Nil)
              case r                            => r
            }
            // Hops run in order; within one, the alternatives compete as before.
            if (resolved.all.isEmpty) emitAndDescend(None, None, Some(c), line, "call")
            else resolved.hops.filter(h => h.methods.nonEmpty || h.provided.nonEmpty).foreach { h =>
              def go(t: Option[Method]) = emitAndDescend(t.map(_.id), t, Some(c), line, h.mechanism,
                bridged = h.bridged, hopVia = h.via, provided = h.provided)
              h.methods match {
                case Nil      => go(None)
                case t :: Nil => go(Some(t))
                case ts       => keepMostRoles(ts.map(t => () => go(Some(t))))
              }
            }

          // A lambda or method reference this body returns, assigns or stores: the code it
          // hands on. Entered where it appears, after what precedes it in this body.
          case mr: MethodRef if !graph.isCallArgument(mr) && !Lang.isModuleOrClassBody(m) =>
            graph.referencedMethodInRepo(mr).foreach { t =>
              val at = mr.lineNumber.orElse(mr.astParent.lineNumber).getOrElse(-1)
              emitAndDescend(Some(t.id), Some(t), None, at, "nested-definition")
            }

          case c: Call if c.name == "<operator>.fieldAccess" =>
            graph.fieldsOf(c).filter(f => roleOfNode.contains(f.id))
              .foreach(f => emitAndDescend(Some(f.id), None, None, line, "data", name = f.name))

          case i: Identifier =>
            i._refOut.collect { case d: AstNode => d }
              .filter(d => roleOfNode.contains(d.id))
              .foreach(d => emitAndDescend(Some(d.id), None, None, line, "data", name = i.name))

          case _ => ()
        }

        n._cfgOut.collect { case c: CfgNode => c }.toList.reverse.foreach(pending.push)
      }
    }
  }
}


// ── 5. emit ──────────────────────────────────────────────────────────────────

/** Each (source, target) once, at the first place the walk reached it. */
def firstOccurrences(edges: List[Edge]): List[Edge] = {
  val seen = scala.collection.mutable.Set[String]()
  edges.filter(e => seen.add(s"${e.source} → ${e.target}"))
}

/** The result JSON; `anchoredRoles` says how and where each role was located. */
def render(graph: CallGraph, located: List[RoleNode], start: Entry,
           interactions: List[Edge]): ujson.Obj =
  ujson.Obj(
    "schemaVersion" -> 2,
    "lang"          -> Lang.id,
    "entryRole"     -> start.role,
    "entryRule"     -> start.rule,
    "anchoredRoles" -> ujson.Arr.from(located.map { a =>
      ujson.Obj("role" -> a.role,
                "kind" -> (if (a.external) "external"
                           else a.decls.headOption.map(_.kind).getOrElse("none")),
                "bodies" -> a.decls.flatMap(graph.startsOf).size,
                "by" -> a.locatedBy,
                "file" -> a.decls.headOption.map(_.file)
                  .orElse(a.libraryCalls.headOption.map(c => graph.repo.file(c.method.filename))).getOrElse(""),
                "line" -> a.decls.headOption.map(_.line)
                  .orElse(a.libraryCalls.headOption.flatMap(_.lineNumber)).getOrElse(-1))
    }),
    "interactions"  -> ujson.Arr.from(interactions.zipWithIndex.map { case (e, i) =>
      ujson.Obj(
        "rank" -> (i + 1),
        "interaction" -> s"${e.source} → ${e.target}",
        "from" -> e.source,
        "to" -> e.target,
        "file" -> e.file,
        "line" -> e.line,
        "via" -> ujson.Arr.from(e.via),
        "mechanism" -> e.mechanism,
        "path" -> ujson.Arr.from(e.path.map { s =>
          val o = ujson.Obj("caller" -> s.caller, "file" -> s.file, "line" -> s.line,
                            "code" -> s.code, "callee" -> s.callee, "mechanism" -> s.mechanism)
          if (s.route.nonEmpty) o("route") = ujson.Str(s.route)
          o
        }),
        "parent" -> e.parent.map { case (a, b) => ujson.Str(s"$a → $b") }.getOrElse(ujson.Null)
      )
    })
  )

def writeResult(outputPath: String, out: ujson.Obj): Unit =
  java.nio.file.Files.write(java.nio.file.Paths.get(outputPath), out.render().getBytes("UTF-8"))
