/* Python profile for walk.sc. Reasoning in JOERN.md § python.sc. */
object Lang extends LangSpec {

  val id = "PYTHONSRC"

  /** Equality: PYTHONSRC puts a method's line on the `def`. */
  def anchorsAt(cited: Int, line: Int, lineEnd: Int): Boolean = cited == line

  /** Candidate files for a dotted module path, read as a module and as a symbol
    * inside one; a leading `.` is relative to `fromFile`. */
  def moduleFiles(entity: String, fromFile: String): List[String] = {
    val dots = entity.takeWhile(_ == '.').length
    val rest = entity.drop(dots)
    val base =
      if (dots == 0) ""
      else fromFile.split('/').dropRight(1).toList.dropRight(math.max(0, dots - 1)).mkString("/")
    val parts = rest.split('.').toList.filter(_.nonEmpty)
    val modules = List(parts, parts.dropRight(1)).filter(_.nonEmpty).distinct
    modules.flatMap { mp =>
      val path =
        (if (base.isEmpty) mp else base.split('/').toList.filter(_.nonEmpty) ++ mp).mkString("/")
      List(path + ".py", path + "/__init__.py")
    }.distinct
  }

  def declKindRank(kind: String): Int = kind match {
    case "METHOD"    => 0
    case "TYPE_DECL" => 1
    case "PARAM"     => 2
    case "LOCAL"     => 3
    case "MEMBER"    => 4
    case _           => 5
  }

  /** `file.py:<module>.Owner.member`: containment is a dotted prefix. */
  def declares(owner: String, member: String): Boolean =
    member.startsWith(owner + ".")

  val invokedOnInstance = List("__call__")

  /** A file's top level is `<module>`, a class body `<body>`; each binds its `def`s as
    * `name = <METHOD_REF>` without running them. */
  def isModuleOrClassBody(m: Method): Boolean = m.name == "<module>" || m.name == "<body>"

  /** Off: an untyped receiver names no declared type to find overrides under. */
  val dispatchesToOverrides = false


  // ── imports ────────────────────────────────────────────────────────────────

  def importedDefinitions(file: String, name: String, root: String): List[Long] =
    index(root).chain(file, name, Set.empty)

  /** A bare call `f()` (its receiver is the identifier `f`), resolved through the file's
    * import; any other call, by the first of: the type it addresses (`declaredTypeCallees`),
    * the module it is read from (`moduleMemberCallees`), the classes the repository stores
    * in the field it is read from (`fieldValueCallees`). */
  def importedCallees(c: Call, root: String): List[Long] =
    cached(index(root).calleeMemo, c.id)(importedCalleesOf(c, root))

  private def importedCalleesOf(c: Call, root: String): List[Long] = {
    val bare = c.receiver.l match {
      case Nil                     => true
      case (i: Identifier) :: Nil  => i.name == c.name
      case _                       => false
    }
    if (bare) importedDefinitions(index(root).rl(c.method.filename), c.name, root)
    else Iterator(
      () => declaredTypeCallees(c, root),
      () => moduleMemberCallees(c, root),
      () => fieldValueCallees(c, root),
      () => moduleVariableCallees(c, root),
      () => localValueCallees(c, root)
    ).map(_()).find(_.nonEmpty).getOrElse(Nil)
  }

  /** The object a method call is made on: `obj` in `obj.m()`. PYTHONSRC reads a compound
    * object into a temporary first (`tmp1 = self.parent; tmp1.m()`), so a temporary is
    * followed back to the expression assigned to it in the same method. */
  private def callObject(c: Call): Option[AstNode] =
    c.receiver.l match {
      case (fa: Call) :: Nil if fa.name == "<operator>.fieldAccess" =>
        fa.argument.l.find(_.argumentIndex == 1).map {
          case t: Identifier if t.name.startsWith("tmp") =>
            c.method.call.nameExact("<operator>.assignment").l.collectFirst {
              case a if a.argument.l.find(_.argumentIndex == 1).exists {
                case i: Identifier => i.name == t.name
                case _             => false
              } => a.argument.l.find(_.argumentIndex == 2)
            }.flatten.getOrElse(t)
          case other => other
        }
      case _ => None
    }

  /** `mod.f()` where `mod` is a module the file imports (`from pkg import mod`,
    * `import pkg.mod as mod`): `f` as that module's file defines it, or re-exports it.
    * PYTHONSRC addresses such a call to a `mod` member of the package file and marks it
    * external, so without this the call reaches no repository code. */
  private def moduleMemberCallees(c: Call, root: String): List[Long] =
    callObject(c) match {
      case Some(i: Identifier) =>
        val ix = index(root)
        val file = ix.rl(c.method.filename)
        ix.importedIn.get((file, i.name)).toList.flatMap { entity =>
          // The entity read as a module (`pkg/mod.py`, `pkg/mod/__init__.py`) -- the first
          // two of `moduleFiles`; the rest read it as a symbol inside its parent.
          moduleFiles(entity, file).take(2).flatMap(ix.toGraphFile).distinct.flatMap { f =>
            val defined = ix.moduleDefs.getOrElse((f, c.name), Nil)
            if (defined.nonEmpty) defined else ix.chain(f, c.name, Set.empty)
          }
        }.distinct
      case _ => Nil
    }

  /** Upper bound on the classes `fieldValueCallees` resolves one field to. */
  private val MAX_FIELD_CLASSES = 3

  /** List, tuple and set displays: iterating one yields its items. */
  private val ITERABLE_LITERALS =
    Set("<operator>.listLiteral", "<operator>.tupleLiteral", "<operator>.setLiteral")

  /** `self.x.m()` where the graph leaves `x` untyped: `m` on each repository class the
    * code stores in `x` -- by a write's annotation (`self.x: T = ...`), its right side
    * (`Owner.x = self`, `self.x = T(...)`, a typed value), on the reading class or one of
    * its bases. Every target is a write in the repository; none is guessed by name. */
  private def fieldValueCallees(c: Call, root: String): List[Long] =
    callObject(c) match {
      case Some(fa: Call) if fa.name == "<operator>.fieldAccess" =>
        val ix = index(root)
        val field = fa.argument.l.collectFirst { case f: FieldIdentifier => f.canonicalName }
        val owner = fa.argument.l.find(_.argumentIndex == 1).flatMap(ix.classOfObject)
        (for (f <- field; o <- owner) yield {
          val classes = ix.fieldClasses(f, o, elements = false)
          if (classes.isEmpty || classes.sizeIs > MAX_FIELD_CLASSES) Nil
          else classes.flatMap(t => ix.definitionUp(t, c.name).toList ++ ix.overridesDown(t, c.name))
            .distinct.map(_.id)
        }).getOrElse(Nil)
      case _ => Nil
    }

  /** `mod.x.m()` where `mod` is a module the file imports, or `x.m()` where the file
    * imports `x` by name (or assigns it at its own top level), and `x` is a module-level
    * variable: `m` on each repository class `x` holds (`classesHeld`). The FastAPI
    * template's `crud.user.authenticate()` -- `user = CRUDUser(User)` in `crud_user.py`,
    * re-exported by `crud/__init__.py` -- is this, and PYTHONSRC addresses it to a
    * member of a module file, so the call reached no repository code. */
  private def moduleVariableCallees(c: Call, root: String): List[Long] = {
    val ix = index(root)
    val file = ix.rl(c.method.filename)
    val variables: List[Long] = callObject(c) match {
      case Some(fa: Call) if fa.name == "<operator>.fieldAccess" =>
        val field = fa.argument.l.collectFirst { case f: FieldIdentifier => f.canonicalName }
        (fa.argument.l.find(_.argumentIndex == 1), field) match {
          case (Some(mod: Identifier), Some(f)) =>
            ix.importedIn.get((file, mod.name)).toList.flatMap { entity =>
              moduleFiles(entity, file).take(2).flatMap(ix.toGraphFile).distinct.flatMap { g =>
                val defined = ix.moduleDefs.getOrElse((g, f), Nil)
                if (defined.nonEmpty) defined else ix.chain(g, f, Set.empty)
              }
            }
          case _ => Nil
        }
      case Some(x: Identifier) if !boundInFunction(x) =>
        val imported = ix.chain(file, x.name, Set.empty)
        if (imported.nonEmpty) imported else ix.moduleDefs.getOrElse((file, x.name), Nil)
      case _ => Nil
    }
    val classes = variables.flatMap(ix.moduleLocalsById.get).flatMap(ix.classesHeld(_, root)).distinct
    if (classes.isEmpty || classes.sizeIs > MAX_FIELD_CLASSES) Nil
    else classes.flatMap(t => ix.definitionUp(t, c.name).toList ++ ix.overridesDown(t, c.name))
      .distinct.map(_.id)
  }

  /** `x.m()` where `x` is a variable or parameter of the calling function: `m` on each
    * repository class the function's own assignments to `x` hold, or its parameter's
    * annotation (`classesOfValue`). A `for` loop's variable is one of these: PYTHONSRC
    * writes `for b in self.backends` as `tmp0 = {tmp1 = self.backends; tmp1.__iter__()}`
    * then `b = tmp0.__next__()`, and `__next__` asks for the elements of what is iterated. */
  private def localValueCallees(c: Call, root: String): List[Long] =
    callObject(c) match {
      case Some(x: Identifier) if boundInFunction(x) =>
        val ix = index(root)
        val classes = ix.classesOfValue(x, elements = false)
        if (classes.isEmpty || classes.sizeIs > MAX_FIELD_CLASSES) Nil
        else classes.flatMap(t => ix.definitionUp(t, c.name).toList ++ ix.overridesDown(t, c.name))
          .distinct.map(_.id)
      case _ => Nil
    }

  /** A name the enclosing function binds itself -- a parameter, or a local that is not a
    * capture of an outer one -- and so does not refer to the module's variable. */
  private def boundInFunction(i: Identifier): Boolean =
    i._refOut.exists {
      case _: MethodParameterIn => true
      case l: Local => !l.method.name.headOption.contains("<module>") &&
        (l.closureBindingId.isEmpty || assignedLocally(l))
      case _ => false
    }

  /** Whether the function assigns `l` itself. PYTHONSRC gives some of a function's own
    * locals a closure binding id (a `for` variable in an `async def`), so the id alone
    * does not say the name is captured from an outer scope; a capture is never assigned. */
  private def assignedLocally(l: Local): Boolean =
    l._refIn.collect { case t: Identifier => t }.exists { t =>
      t.astParent match {
        case a: Call => a.name == "<operator>.assignment" && a.argument.l.find(_.argumentIndex == 1).exists(_.id == t.id)
        case _       => false
      }
    }

  /** `svc.m()` addressed as `T.m`, the annotation's bare name: `T` through the file's
    * import (else defined in the file), then every `m` a run could land on -- the
    * first definition up `T`'s bases, then each subclass override, nearest first.
    * Also `T.m()` addressed under a file the graph does not have, `f.py:<module>.T.m`:
    * the frontend read the import's module path wrongly (a relative import as
    * `//pkg/mod.py`, a `src/` layout without `src/`), but `T` is still the name the
    * file imports, so it is bound the same way. */
  private def declaredTypeCallees(c: Call, root: String): List[Long] = {
    val k = c.methodFullName.lastIndexOf('.')
    val addressed = if (k > 0) c.methodFullName.substring(0, k) else ""
    def isName(s: String) = s.nonEmpty && s.forall(ch => ch.isLetterOrDigit || ch == '_')
    val typeName =
      if (isName(addressed)) addressed
      else """^.+\.py:<module>\.([A-Za-z_][A-Za-z0-9_]*)$""".r.findFirstMatchIn(addressed)
        .map(_.group(1)).filterNot(_ => index(root).repoClasses(addressed)).getOrElse("")
    if (typeName.isEmpty || c.methodFullName.substring(k + 1) != c.name) Nil
    else {
      val ix = index(root)
      val file = ix.rl(c.method.filename)
      val imported = ix.chain(file, typeName, Set.empty)
      val ids = if (imported.nonEmpty) imported else ix.moduleDefs.getOrElse((file, typeName), Nil)
      ids.flatMap(ix.typeFullNameById.get).distinct match {
        case List(t) => (ix.definitionUp(t, c.name).toList ++ ix.overridesDown(t, c.name)).distinct.map(_.id)
        case _       => Nil
      }
    }
  }

  /** `self.x` / `cls.x`: the class the method is defined in. */
  def fieldOwnerType(access: Call, root: String): Option[String] =
    access.argument.l.find(_.argumentIndex == 1).collect { case i: Identifier => i }
      .flatMap(_._refOut.collectFirst { case p: MethodParameterIn if p.index == 0 => p })
      .flatMap { p =>
        val fn = p.method.fullName
        val owner = fn.substring(0, math.max(0, fn.lastIndexOf('.')))
        Option.when(owner.nonEmpty && !owner.endsWith("<module>"))(owner)
          .filter(o => cpg.typeDecl.fullNameExact(o).nonEmpty)
      }

  /** Nil: PYTHONSRC records no methods a field's declaration generates. */
  def accessorsOf(field: Member, root: String): List[Long] = Nil

  /** Nil, for the same reason. */
  def fieldOfAccessor(accessor: Method, root: String): List[Long] = Nil

  /** A LOCAL assigned by `import(...)`, or a closure capture of a name the file imports. */
  def isImportBinding(local: Local, root: String): Boolean =
    assignedByImport(local) || (local.closureBindingId.nonEmpty && {
      val ix = index(root)
      ix.importedIn.contains((ix.rl(local.method.filename.headOption.getOrElse("")), local.name))
    })

  /** Whether some assignment to `local` has an `import` call on its right side. */
  private def assignedByImport(local: Local): Boolean =
    local._refIn.collect { case i: Identifier => i }.exists { target =>
      target.astParent match {
        case a: Call if a.name == "<operator>.assignment" =>
          val args = a.argument.l
          args.headOption.exists(_.id == target.id) &&
            args.drop(1).exists { case c: Call => c.name == "import" || c.name == "<operator>.import"; case _ => false }
        case _ => false
      }
    }


  // ── dependency injection ───────────────────────────────────────────────────

  /** Methods named in `m`'s parameter defaults (`Depends(f)`), read from source. */
  def entryEdges(m: Method, root: String): List[(Method, Int, String)] = {
    val relFile = norm(rel(m.filename, root))
    val start = m.lineNumber.getOrElse(-1)
    if (relFile.isEmpty || start < 0) Nil
    else {
      val sig = signature(linesOf(root, relFile), start)
      if (!sig.contains("=")) Nil
      else m.parameter.l.flatMap(p => defaultsIn(sig, p.name)).distinct
        .flatMap(name => methodForName(name, relFile, root))
        .map(t => (t, start, "dependency-injection"))
    }
  }

  /** The one class name an annotation states, or None for anything that is not one class:
    * quotes, `Optional[...]` and a `| None` arm are stripped; a dotted name keeps its last
    * segment; generics (`list[T]`) and real unions stay unread. */
  private def annotationClass(expr: String): Option[String] = {
    val bare = expr.trim.replaceAll("""["']""", "")
    val inner = """^(?:typing\.)?Optional\[(.*)\]$""".r.findFirstMatchIn(bare).map(_.group(1)).getOrElse(bare)
    inner.split('|').map(_.trim).filterNot(p => p == "None" || p.isEmpty).toList match {
      case List(one) if one.matches("""[A-Za-z_][\w.]*""") => Some(one.split('.').last)
      case _ => None
    }
  }

  /** `m(k)`, computing and storing it on first use. Not `getOrElseUpdate`: a computation
    * may itself fill the same table (`methodForName` recurses). */
  private def cached[K, V](m: scala.collection.mutable.Map[K, V], k: K)(f: => V): V =
    m.get(k) match {
      case Some(v) => v
      case None    => val v = f; m(k) = v; v
    }

  private def norm(p: String): String = p.replace('\\', '/').stripPrefix("./").stripPrefix("/")
  private def rel(p: String, root: String): String = {
    val r = if (root.isEmpty || root.endsWith("/")) root else root + "/"
    val s = if (r.nonEmpty && p.startsWith(r)) p.substring(r.length) else p
    if (s == "<empty>" || s == "N/A" || s == "<unknown>") "" else s
  }

  private val srcCache = scala.collection.mutable.Map[String, Vector[String]]()
  /** A source file's lines, cached. */
  private def linesOf(root: String, relPath: String): Vector[String] =
    srcCache.getOrElseUpdate(relPath, scala.util.Try(
      scala.jdk.CollectionConverters.ListHasAsScala(
        java.nio.file.Files.readAllLines(java.nio.file.Paths.get(
          (if (root.isEmpty || root.endsWith("/")) root else root + "/") + relPath))
      ).asScala.toVector).getOrElse(Vector.empty))

  /** The `def` signature starting at `startLine`, joined until the parens balance. */
  private def signature(lines: Vector[String], startLine: Int): String = {
    if (startLine < 1 || startLine > lines.size) return ""
    var depth = 0; var i = startLine - 1; var started = false
    val sb = new StringBuilder
    while (i < lines.size && (!started || depth > 0)) {
      val l = lines(i)
      l.foreach { c => if (c == '(') { depth += 1; started = true } else if (c == ')') depth -= 1 }
      sb.append(l.trim); i += 1
      if (started && depth <= 0) i = lines.size
    }
    sb.toString
  }

  /** Every identifier in `param`'s default expression. The parameter list is split at
    * its top-level commas and `param` is the entry whose own name it is, so an
    * annotation's brackets (`tuple[User, APIKey]`, `Annotated[T, Depends(f)]`) cannot end
    * the entry early, nor can `token` match inside `bearer_token`. */
  private def defaultsIn(sig: String, param: String): List[String] =
    parameterList(sig).find(entryName(_) == param).map(topLevelSplit(_, '=')).filter(_.size > 1)
      .map(_.drop(1).mkString("=")).toList.flatMap(expr => namesIn(expr))

  /** The names an expression refers to: not the words of its string literals
    * (`Query(None, description="Filter by user id")`), nor its keyword-argument names. */
  private def namesIn(expr: String): List[String] = {
    val code = """(?s)[rbfRBF]*(\"\"\".*?\"\"\"|'''.*?'''|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')""".r
      .replaceAllIn(expr, " ")
    val keywords = """\b[A-Za-z_][A-Za-z0-9_]*\s*=(?!=)""".r.replaceAllIn(code, " ")
    """[A-Za-z_][A-Za-z0-9_]*""".r.findAllIn(keywords).toList.distinct
  }

  /** The entries of the signature's first parenthesised list, split at top-level commas. */
  private def parameterList(sig: String): List[String] = {
    val open = sig.indexOf('(')
    if (open < 0) Nil
    else {
      var depth = 0; var i = open + 1; var close = sig.length
      while (i < sig.length && close == sig.length) {
        sig.charAt(i) match {
          case '(' | '[' | '{' => depth += 1
          case ')' | ']' | '}' => if (depth == 0) close = i else depth -= 1
          case _ => ()
        }
        i += 1
      }
      topLevelSplit(sig.substring(open + 1, close), ',').map(_.trim).filter(_.nonEmpty)
    }
  }

  /** `s` split at each `sep` outside brackets; a `=` beside another `=`, `<`, `>` or `!` is
    * a comparison, not a split. */
  private def topLevelSplit(s: String, sep: Char): List[String] = {
    val parts = scala.collection.mutable.ListBuffer[String]()
    var depth = 0; var from = 0
    for (i <- 0 until s.length) s.charAt(i) match {
      case '(' | '[' | '{' => depth += 1
      case ')' | ']' | '}' => depth -= 1
      case c if c == sep && depth == 0 &&
          !(sep == '=' && ("=<>!".contains(s.lift(i - 1).getOrElse(' ')) || s.lift(i + 1).contains('='))) =>
        parts += s.substring(from, i); from = i + 1
      case _ => ()
    }
    (parts += s.substring(from)).toList
  }

  /** A parameter entry's name: `*args`, `**kw`, `x: T = d` -> `args`, `kw`, `x`. */
  private def entryName(entry: String): String =
    """^\**\s*([A-Za-z_][A-Za-z0-9_]*)""".r.findFirstMatchIn(entry.trim).map(_.group(1)).getOrElse("")


  // ── indexes, built on first use per root ───────────────────────────────────

  private class Index(root: String) {
    def rl(p: String) = norm(rel(p, root))

    /** Memo tables. Every answer below is a pure function of the graph, and the walk asks
      * the same questions again for each call and method it passes; uncached, a large
      * repository (`amphetamarina_litellm`) ran past the tunnel's 100 s. */
    val calleeMemo = scala.collection.mutable.Map[Long, List[Long]]()
    val nameMemo = scala.collection.mutable.Map[(String, String, Int), List[Method]]()
    val namedTypes = scala.collection.mutable.Map[String, List[String]]()
    val objectClassMemo = scala.collection.mutable.Map[Long, Option[String]]()
    val heldMemo = scala.collection.mutable.Map[Long, List[String]]()
    val fieldClassMemo = scala.collection.mutable.Map[(String, String, Boolean), List[String]]()
    val valueMemo = scala.collection.mutable.Map[(Long, Boolean), List[String]]()
    private val inProgress = scala.collection.mutable.Set[Any]()

    /** `cached`, answering Nil to a lookup already under way (a cycle). */
    def guarded[K, V](m: scala.collection.mutable.Map[K, List[V]], k: K)(f: => List[V]): List[V] =
      m.get(k) match {
        case Some(v)                      => v
        case None if !inProgress.add(k)   => Nil
        case None =>
          try { val v = f; m(k) = v; v } finally inProgress.remove(k)
      }
    lazy val allMethods = cpg.method.l
    lazy val internal = allMethods.filterNot(_.isExternal).filter(m => rl(m.filename).nonEmpty)

    /** (file, local name) -> the dotted entity an import bound it to. */
    lazy val importedIn: Map[(String, String), String] =
      cpg.imports.l.flatMap { i =>
        val f = rl(i.file.name.headOption.getOrElse(""))
        val as = i.importedAs.getOrElse("")
        val ent = i.importedEntity.getOrElse("")
        if (f.isEmpty || as.isEmpty || ent.isEmpty) None else Some((f, as) -> ent)
      }.toMap

    lazy val methodsInFile: Map[String, List[Method]] = internal.groupBy(m => rl(m.filename))
    lazy val methodsNamed: Map[String, List[Method]] =
      internal.filterNot(m => isSynthetic(m.name)).groupBy(_.name)

    /** Methods by owner: the fullName up to the last `.`. */
    lazy val methodsByOwner: Map[String, List[Method]] =
      allMethods.flatMap { m =>
        val k = m.fullName.lastIndexOf('.')
        if (k <= 0) None else Some(m.fullName.substring(0, k) -> m)
      }.groupBy(_._1).map { case (k, v) => k -> v.map(_._2) }

    lazy val typeFullNameById: Map[Long, String] =
      cpg.typeDecl.isExternal(false).l.map(t => t.id -> t.fullName).toMap

    /** Type fullName -> its bases, in declared order. */
    lazy val basesOf: Map[String, List[String]] =
      cpg.typeDecl.isExternal(false).l.map(t => t.fullName -> t.inheritsFromTypeFullName.l).toMap

    /** Type fullName -> the repository types naming it as a base. */
    lazy val subclassesOf: Map[String, List[String]] =
      basesOf.toList.flatMap { case (t, bs) => bs.map(_ -> t) }
        .groupBy(_._1).map { case (b, v) => b -> v.map(_._2).distinct.sorted }

    /** `name` as declared directly in the body of type `t`. */
    def definedIn(t: String, name: String): Option[Method] =
      methodsByOwner.getOrElse(t, Nil).find(m => m.name == name && !m.isExternal)

    /** The first `name` found from `t` up its bases, depth-first in declared order --
      * the MRO for single inheritance, an approximation of it otherwise. */
    def definitionUp(t: String, name: String): Option[Method] = {
      val seen = scala.collection.mutable.Set[String]()
      def up(x: String): Option[Method] =
        if (!seen.add(x)) None
        else definedIn(x, name).orElse(basesOf.getOrElse(x, Nil).iterator.map(up).collectFirst { case Some(m) => m })
      up(t)
    }

    /** Repository classes: TYPE_DECLs declared in a source file (`file.py:<module>.Name`).
      * PYTHONSRC also gives every function a TYPE_DECL, each class a `<meta>` twin, and
      * keeps placeholders such as `ANY` internal; none of those is a class. */
    lazy val repoClasses: Set[String] =
      typeFullNameById.values.filter(t => t.contains(":<module>.") && !t.endsWith("<meta>")).toSet --
        allMethods.map(_.fullName).toSet

    /** The class whose body `m` is written in, at any depth (a closure inside a method
      * belongs to the method's class): the longest fullName prefix that is a class. */
    def classAround(m: Method): Option[String] =
      Iterator.iterate(m.fullName)(fn => fn.substring(0, math.max(0, fn.lastIndexOf('.'))))
        .takeWhile(_.nonEmpty).drop(1).find(repoClasses)

    /** The class an object expression stands for: a typed name, else `self` / `cls`
      * (a method's first parameter, or a closure's capture of it) as the class around it,
      * else an annotated name. PYTHONSRC types an annotated parameter (`crud: CRUDInterface`)
      * with the annotation's bare text, not the class's fullName, so that text is bound
      * through the file's import (re-exports included) or a class the file defines. */
    def classOfObject(n: AstNode): Option[String] =
      cached(objectClassMemo, n.id)(classOfObjectUncached(n))

    private def classOfObjectUncached(n: AstNode): Option[String] = n match {
      case i: Identifier if repoClasses(i.typeFullName) => Some(i.typeFullName)
      case i: Identifier if i.name == "self" || i.name == "cls" ||
          i._refOut.exists { case p: MethodParameterIn => p.index == 0; case _ => false } =>
        classAround(i.method)
      case i: Identifier =>
        val file = rl(i.method.filename)
        val typed = annotationClass(i.typeFullName).toList.flatMap(classNamed(file, _))
        // PYTHONSRC types `x: T | None` as ANY and `x: Optional[T]` as typing.Optional:
        // the parameter's annotation is then read from the signature itself.
        val declared = if (typed.nonEmpty) typed else
          i._refOut.collectFirst { case p: MethodParameterIn => p }.toList
            .flatMap(sourceAnnotation).flatMap(annotationClass).flatMap(classNamed(file, _))
        declared.distinct match {
          case List(t) => Some(t)
          case _       => None
        }
      case _ => None
    }

    /** A parameter's annotation as written in its method's signature, or None. */
    private def sourceAnnotation(p: MethodParameterIn): Option[String] = {
      val m = p.method
      val file = rl(m.filename)
      m.lineNumber.flatMap { start =>
        parameterList(signature(linesOf(root, file), start)).find(entryName(_) == p.name)
          .map(entry => topLevelSplit(entry, '=').head)
          .filter(_.contains(':')).map(head => head.substring(head.indexOf(':') + 1).trim)
          .filter(_.nonEmpty)
      }
    }

    /** Every base of `t`, transitively. */
    def ancestors(t: String): Set[String] = {
      val seen = scala.collection.mutable.Set[String]()
      def up(x: String): Unit = basesOf.getOrElse(x, Nil).foreach(b => if (seen.add(b)) up(b))
      up(t); seen.toSet
    }

    /** Field name -> every assignment whose target is `obj.<field>`. */
    lazy val fieldWrites: Map[String, List[Call]] =
      cpg.call.nameExact("<operator>.assignment").l.flatMap { a =>
        a.argument.l.find(_.argumentIndex == 1).collect {
          case lhs: Call if lhs.name == "<operator>.fieldAccess" =>
            lhs.argument.l.collectFirst { case f: FieldIdentifier => f.canonicalName }.map(_ -> a)
        }.flatten
      }.groupBy(_._1).map { case (k, v) => k -> v.map(_._2) }

    /** A class `name` as `file` binds it: imported, else defined in the file. */
    def classNamed(file: String, name: String): List[String] = {
      val imported = chain(file, name, Set.empty)
      val ids = if (imported.nonEmpty) imported else moduleDefs.getOrElse((file, name), Nil)
      ids.flatMap(typeFullNameById.get).filter(repoClasses).distinct
    }

    /** The repository classes a write `obj.<field> = value` stores: the annotation on its
      * line (`self.x: T = ...`, `Optional[T]` and `T | None` read as `T`), else the value
      * -- `self` / a typed name (`classOfObject`), or a constructor call `T(...)`. */
    def classesWritten(w: Call, field: String, root: String): List[String] = {
      val file = rl(w.method.filename)
      val annotated = w.lineNumber.toList.flatMap { line =>
        linesOf(root, file).lift(line - 1).toList.flatMap { text =>
          ("""\.""" + java.util.regex.Pattern.quote(field) + """\s*:\s*([^=]+?)\s*=""").r
            .findFirstMatchIn(text).toList.flatMap(m => annotationClass(m.group(1)))
        }.flatMap(classNamed(file, _))
      }
      if (annotated.nonEmpty) annotated.distinct
      else w.argument.l.find(_.argumentIndex == 2).toList.flatMap(classesOfValue(_, elements = false)).distinct
    }

    /** The classes the repository stores in `owner.field` (or a base's): every write's
      * annotation or value (`classesWritten`), or with `elements`, what iterating the
      * stored value yields. */
    def fieldClasses(field: String, owner: String, elements: Boolean): List[String] =
      guarded(fieldClassMemo, (field, owner, elements)) {
        val owners = ancestors(owner) + owner
        fieldWrites.getOrElse(field, Nil)
          .filter(w => w.argument.l.find(_.argumentIndex == 1).exists {
            case lhs: Call => lhs.argument.l.find(_.argumentIndex == 1).flatMap(classOfObject).exists(owners)
            case _         => false
          })
          .flatMap { w =>
            if (elements) w.argument.l.find(_.argumentIndex == 2).toList.flatMap(classesOfValue(_, elements = true))
            else classesWritten(w, field, root)
          }.distinct
      }

    /** The repository classes an expression evaluates to -- or, with `elements`, the
      * classes iterating it yields. Read from what the repository writes, never guessed:
      * a constructor call `T(...)`, a typed or annotated name, either side of `a or b` /
      * `a if p else b`, the items of a list, tuple or set, a field (`fieldClasses`), a
      * local's own assignments, a block's last expression; `x.__iter__()` is `x`, and
      * `x.__next__()` is the elements of `x`. A cycle (`self.x = self.x or T()`) answers
      * nothing for the inner read. */
    def classesOfValue(v: AstNode, elements: Boolean): List[String] =
      guarded(valueMemo, (v.id, elements))(valueClasses(v, elements))

    private def valueClasses(v: AstNode, elements: Boolean): List[String] = {
      def of(n: AstNode, el: Boolean = elements) = classesOfValue(n, el)
      def arg(c: Call, i: Int) = c.argument.l.find(_.argumentIndex == i)
      (v match {
        case b: Block => b.astChildren.l.sortBy(_.order).lastOption.toList.flatMap(of(_))
        case c: Call if c.name == "<operator>.logicalOr" => c.argument.l.flatMap(of(_))
        case c: Call if c.name == "<operator>.conditional" => c.argument.l.filter(_.argumentIndex >= 2).flatMap(of(_))
        case c: Call if c.name == "<operator>.assignment" => arg(c, 2).toList.flatMap(of(_))
        case c: Call if ITERABLE_LITERALS(c.name) => if (elements) c.argument.l.flatMap(of(_, false)) else Nil
        case c: Call if c.name == "<operator>.fieldAccess" =>
          val f = c.argument.l.collectFirst { case f: FieldIdentifier => f.canonicalName }
          val o = arg(c, 1).flatMap(classOfObject)
          (for (f <- f; o <- o) yield fieldClasses(f, o, elements)).getOrElse(Nil)
        case c: Call if c.name == "__iter__" => arg(c, 0).toList.flatMap(of(_))
        case c: Call if c.name == "__next__" => arg(c, 0).toList.flatMap(of(_, true))
        case c: Call if !c.name.startsWith("<operator>") => if (elements) Nil else classNamed(rl(c.method.filename), c.name)
        case i: Identifier =>
          val typed = if (elements) Nil else classOfObject(i).toList
          if (typed.nonEmpty) typed else localWrites(i).flatMap(of(_))
        case _ => Nil
      }).distinct
    }

    /** The values a function's own assignments give the local `i` names (not a parameter,
      * nor a module variable -- those are typed by annotation and by `classesHeld`). */
    private def localWrites(i: Identifier): List[AstNode] =
      i._refOut.collectFirst { case l: Local => l }.filterNot(_.method.name.headOption.contains("<module>"))
        .toList.flatMap(_._refIn.collect { case t: Identifier => t }.toList).flatMap { t =>
          t.astParent match {
            case a: Call if a.name == "<operator>.assignment" &&
                a.argument.l.find(_.argumentIndex == 1).exists(_.id == t.id) =>
              a.argument.l.find(_.argumentIndex == 2).toList
            case _ => Nil
          }
        }

    /** Module-level variables by node id: the LOCALs `moduleDefs` answers for a name a
      * module assigns rather than defines. */
    lazy val moduleLocalsById: Map[Long, Local] =
      cpg.local.l.filter(_.method.name.headOption.contains("<module>")).map(l => l.id -> l).toMap

    /** The repository classes a module-level variable holds, read per assignment as
      * `classesWritten` reads a field's: its annotation (`x: T = ...`), else the value --
      * a constructor call `T(...)` or a name typed as a repository class. */
    def classesHeld(local: Local, root: String): List[String] =
      cached(heldMemo, local.id)(classesHeldUncached(local, root))

    private def classesHeldUncached(local: Local, root: String): List[String] = {
      val file = rl(local.method.filename.headOption.getOrElse(""))
      local._refIn.collect { case i: Identifier => i }.toList.flatMap { target =>
        target.astParent match {
          case a: Call if a.name == "<operator>.assignment" &&
              a.argument.l.find(_.argumentIndex == 1).exists(_.id == target.id) =>
            val annotated = a.lineNumber.toList.flatMap { line =>
              linesOf(root, file).lift(line - 1).toList.flatMap { text =>
                ("""^\s*""" + java.util.regex.Pattern.quote(local.name) + """\s*:\s*([^=]+?)\s*=""").r
                  .findFirstMatchIn(text).toList.flatMap(m => annotationClass(m.group(1)))
              }.flatMap(classNamed(file, _))
            }
            if (annotated.nonEmpty) annotated
            else a.argument.l.find(_.argumentIndex == 2).toList.flatMap(classesOfValue(_, elements = false))
          case _ => Nil
        }
      }.distinct
    }

    /** Every `name` declared by a subclass of `t`, level by level, nearest first. */
    def overridesDown(t: String, name: String): List[Method] = {
      val seen = scala.collection.mutable.Set(t)
      var level = subclassesOf.getOrElse(t, Nil).filter(seen.add)
      val found = scala.collection.mutable.ListBuffer[Method]()
      while (level.nonEmpty) {
        found ++= level.flatMap(definedIn(_, name))
        level = level.flatMap(subclassesOf.getOrElse(_, Nil)).distinct.filter(seen.add)
      }
      found.toList
    }

    /** Type fullNames by fullName and by last path segment. */
    lazy val typeDeclNames: Map[String, List[String]] =
      cpg.typeDecl.l.flatMap { td =>
        val fn = td.fullName
        List(fn -> fn, fn.substring(fn.lastIndexOf('/') + 1) -> fn)
      }.distinct.groupBy(_._1).map { case (k, v) => k -> v.map(_._2).distinct }

    /** Names on the right of each assignment, by (file, target) and by target alone. */
    lazy val assignments = {
      val rows = cpg.call.nameExact("<operator>.assignment").l.flatMap { a =>
        val args = a.argument.l
        val lhs = args.headOption.map {
          case i: Identifier => i.name
          case n             => n.code.trim
        }.getOrElse("")
        if (lhs.isEmpty) Nil
        else {
          val file = rl(a.file.name.headOption.getOrElse(""))
          val rhs = args.drop(1).flatMap(_.ast.l).collect {
            case c: Call if !c.name.startsWith("<operator>") => c.name
            case i: Identifier => i.name
            case mr: io.shiftleft.codepropertygraph.generated.nodes.MethodRef =>
              mr.methodFullName.split("[:.]").lastOption.getOrElse("")
          }.filter(n => n.nonEmpty && n != lhs).distinct
          if (rhs.isEmpty) Nil else List((file, lhs, rhs))
        }
      }
      (rows.groupBy(r => (r._1, r._2)).map { case (k, v) => k -> v.flatMap(_._3).distinct },
       rows.groupBy(_._2).map { case (k, v) => k -> v.flatMap(_._3).distinct })
    }
    lazy val assignedInFile = assignments._1
    lazy val assignedAnywhere = assignments._2

    lazy val graphFiles: Set[String] = cpg.file.name.l.map(rl).filter(_.nonEmpty).toSet

    /** A repo-root path as a graph file: exact, or the unique match below a source root (`src/`). */
    def toGraphFile(p: String): Option[String] =
      if (graphFiles.contains(p)) Some(p)
      else graphFiles.filter { f =>
        f.endsWith("/" + p) && !graphFiles.contains(f.dropRight(p.length) + "__init__.py")
      }.toList match {
        case List(one) => Some(one)
        case _         => None
      }

    /** (file, name) -> module-level `def`, `class` and non-import assignment nodes. */
    lazy val moduleDefs: Map[(String, String), List[Long]] = {
      val top = (cpg.method.l.map(m => (m.filename, m.name, m.fullName, m.id)) ++
                 cpg.typeDecl.l.map(t => (t.filename, t.name, t.fullName, t.id)))
        .filter { case (_, n, fn, _) => fn.endsWith(":<module>." + n) }
        .map { case (f, n, _, id) => (rl(f), n) -> id }
      val locals = cpg.local.l
        .filter(l => l.method.name.headOption.contains("<module>"))
        .filterNot(l => isImportBinding(l, root))
        .map(l => (rl(l.method.filename.headOption.getOrElse("")), l.name) -> l.id)
      (top ++ locals).groupBy(_._1).map { case (k, v) => k -> v.map(_._2).distinct }
    }

    /** Follow `name` as `file` imports it, file by file, until one defines it. */
    def chain(file: String, name: String, seen: Set[(String, String)]): List[Long] =
      if (seen.contains((file, name))) Nil
      else importedIn.get((file, name)).toList.flatMap { entity =>
        val symbol = entity.split('.').filter(_.nonEmpty).lastOption.getOrElse(name)
        moduleFiles(entity, file).flatMap(toGraphFile).distinct.flatMap { f =>
          val defined = moduleDefs.getOrElse((f, symbol), Nil)
          if (defined.nonEmpty) defined else chain(f, symbol, seen + ((file, name)))
        }
      }.distinct
  }

  private var cached: (String, Index) = null
  private def index(root: String): Index = {
    if (cached == null || cached._1 != root) cached = (root, new Index(root))
    cached._2
  }

  /** The method a DI name refers to: a function, an instance's `__call__`, or
    * a variable holding the wiring. */
  private def methodForName(name: String, fromFile: String, root: String,
                            depth: Int = 0): List[Method] =
    cached(index(root).nameMemo, (name, fromFile, depth))(methodForNameOf(name, fromFile, root, depth))

  private def methodForNameOf(name: String, fromFile: String, root: String,
                              depth: Int): List[Method] = {
    if (depth > 2) return Nil
    val ix = index(root)

    val entity = ix.importedIn.get((fromFile, name))
    val homeFiles = entity.map(e => moduleFiles(e, fromFile).flatMap(ix.toGraphFile)).getOrElse(Nil)

    // 1. a function
    val importedIds = ix.chain(fromFile, name, Set.empty).toSet
    val imported = ix.internal.filter(m => importedIds.contains(m.id) && !isSynthetic(m.name))
    val here = ix.methodsInFile.getOrElse(fromFile, Nil)
      .filter(m => m.name == name && !isSynthetic(m.name))
    val scopedHit = imported ++ here
    val anywhere = ix.methodsNamed.getOrElse(name, Nil)
    val asFunction =
      if (scopedHit.nonEmpty) scopedHit.take(1)
      else if (anywhere.sizeIs == 1) anywhere
      else Nil
    if (asFunction.nonEmpty) return asFunction

    // 2. a variable holding an instance
    val typeNames = cached(ix.namedTypes, name)(
      (cpg.local.nameExact(name).typeFullName.l ++ cpg.identifier.nameExact(name).typeFullName.l)
        .filter(t => t.nonEmpty && t != "ANY").distinct)
    val asInstance = typeNames
      .flatMap(short => ix.typeDeclNames.getOrElse(short, Nil))
      .flatMap(fn => ix.methodsByOwner.getOrElse(fn, Nil))
      .filter(m => invokedOnInstance.contains(m.name)).take(1)
    if (asInstance.nonEmpty) return asInstance

    // 3. a variable holding the wiring
    val scoped = (homeFiles :+ fromFile)
      .flatMap(f => ix.assignedInFile.getOrElse((f, name), Nil)).distinct
    val assigned = if (scoped.nonEmpty) scoped else ix.assignedAnywhere.getOrElse(name, Nil)

    assigned.flatMap(n => methodForName(n, fromFile, root, depth + 1)).distinct
  }
}
