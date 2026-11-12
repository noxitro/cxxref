// 段階 1 の索引生成: compile_commands.json の TU を libtooling の clang::index で走査し、
// プロジェクト内の参照を TSV に出す。
//   記号 <TAB> 役割 <TAB> 呼び出し元 <TAB> path:line:col <TAB> ソース行
// 役割 dep は、テンプレート内で解決先が決まらない呼び出し・参照(記号は ?::名前)。
// 役割の接尾辞 -using は、テンプレート内の using 宣言越しの参照を、実体化から引いた宣言の先で出したもの。
// 役割 override は仮想関数の上書き。3 列目は呼び出し元ではなく、上書きされる基底のメソッド。
// 役割の接尾辞 -dynamic は、限定名の無い仮想関数の呼び出し(実行時に受け手の型の上書きが動く)。-static は、それ以外の
// 仮想関数の呼び出し(Base::f()・Super::f()。その関数そのものを呼ぶ)。
// TU ごとの成否は別ファイル(<out>.tu.tsv)に、TU が読んだルート配下のファイルは <out>.deps.tsv に出す。
// 役割 macro・expand・ifdef・undef は、マクロの #define・展開・#ifdef と defined()・#undef(記号はマクロの名前)。
// コメントと無効な #if の範囲、展開で捨てられたマクロの引数の範囲(dropped)、関数の中の変数・引数とテンプレートの引数の
// 名前の位置(local)も別ファイル(<out>.skip.tsv)に出す。テキスト検索の一致が
// 索引に無い理由を、問い合わせ側(ltq)が答えるのに使う。
// -update は、前回の索引の後に変わったファイルを読む TU・コマンドの変わった TU・新しい TU だけを解析し直して差し替える。
// 複数の TU が読むファイルの行の出どころは <out>.own.bin に出す(-update で、解析し直す TU だけが出していた行を消すのに使う)。
#include "clang/AST/ASTContext.h"
#include "clang/AST/Decl.h"
#include "clang/AST/DeclTemplate.h"
#include "clang/AST/ExprCXX.h"
#include "clang/AST/RecursiveASTVisitor.h"
#include "clang/Basic/SourceManager.h"
#include "clang/Frontend/CompilerInstance.h"
#include "clang/Frontend/FrontendAction.h"
#include "clang/Frontend/MultiplexConsumer.h"
#include "clang/Index/IndexDataConsumer.h"
#include "clang/Index/IndexingAction.h"
#include "clang/Index/IndexSymbol.h"
#include "clang/Lex/MacroArgs.h"
#include "clang/Lex/PPCallbacks.h"
#include "clang/Lex/Preprocessor.h"
#include "clang/Tooling/ArgumentsAdjusters.h"
#include "clang/Tooling/CompilationDatabase.h"
#include "clang/Tooling/Tooling.h"
#include "llvm/ADT/DenseSet.h"
#include "llvm/ADT/StringExtras.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/ConvertUTF.h"
#include "llvm/Support/Endian.h"
#include "llvm/Support/EndianStream.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/Path.h"
#include "llvm/Support/VirtualFileSystem.h"
#include "llvm/Support/raw_ostream.h"
#include "llvm/Support/thread.h"
#include "llvm/Support/xxhash.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <functional>
#include <map>
#include <mutex>
#include <set>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

bool codepageToUtf8(unsigned Cp, const char *P, size_t N, std::string &Out);  // codepage.cpp
unsigned ansiCodepage();
std::string shortPath(const std::string &P);  // 8.3 形式の短い名前(取れなければそのまま)

// clang/Driver/Driver.h の宣言と同じ(ヘッダーを読むと Options.inc のマクロがぶつかるので、使う 2 つだけを宣言する。clangDriver にある)
namespace clang::driver {
llvm::StringRef getDriverMode(llvm::StringRef ProgName, llvm::ArrayRef<const char *> Args);
bool IsClangCL(llvm::StringRef DriverMode);
}  // namespace clang::driver

using namespace clang;
namespace cl = llvm::cl;

static cl::opt<std::string> CdbPath("p", cl::desc("compile_commands.json のあるディレクトリ"), cl::Required);
static cl::opt<std::string> Root("root", cl::desc("索引に含めるソースのルート(これ以外の場所の参照は捨てる)"), cl::Required);
static cl::opt<std::string> Out("o", cl::desc("出力 TSV"), cl::Required);
static cl::opt<std::string> ResourceDir("resource-dir", cl::desc("clang の組み込みヘッダーの場所"), cl::init(LTIDX_RESOURCE_DIR));
static cl::opt<unsigned> Jobs("j", cl::desc("並列数"), cl::init(4));
static cl::opt<bool> NoInst("no-inst", cl::desc("テンプレートの暗黙の実体化を索引しない(比較用)"));
static cl::opt<bool> Delayed("delayed", cl::desc("MSVC 向けの既定の遅延テンプレート解析のままにする"
                                                 "(/Zc:twoPhase や -fno-delayed-template-parsing を足さない)"));
static cl::opt<bool> NoDep("no-dep", cl::desc("テンプレート内の解決できない呼び出し(dep 行)を出さない"));
static cl::opt<bool> NoLocal("no-local", cl::desc("関数の中の変数・引数とテンプレートの引数の名前の位置(local の範囲)を出さない"));
static cl::opt<bool> Update("update", cl::desc("前回の索引の後に変わった TU だけを解析し直して差し替える"));
static cl::opt<unsigned> SourceCp("source-cp", cl::desc("UTF-8 でないソースを読むコードページ(既定はこの PC の ANSI コードページ。"
                                                        "TU の /source-charset が優先)"), cl::init(0));
static cl::opt<std::string> Overlay("overlay", cl::desc("読むファイルを差し替える表(1 行に「元のパス<TAB>代わりのファイル」)。"
                                                        "無効な #if の中を仮に解析する差分パス(scripts/altpass.py)が使う"));
static cl::opt<bool> KeepGoing("keep-going", cl::desc("見つからない #include などの致命的なエラーをふつうのエラーとして扱い、エラーの数の上限も"
                                                      "外す(差分パス用)。致命的なエラーの後、clang はテンプレートの実体化をやめる"));
static cl::list<std::string> Files(cl::Positional, cl::desc("[対象ファイル...](省略時は全 TU)"));

namespace {

// コマンドラインを clang-cl の形式(cl.exe・clang-cl・clang-cl-17・--driver-mode=cl)で読むか。clang の driver と
// 同じ関数で決める(最後の --driver-mode=、無ければ実行ファイルの名前)。gcc 形式では /Zc:twoPhase はファイル名に取られる
bool isClDriver(const tooling::CommandLineArguments &A) {
  if (A.empty()) return false;
  std::vector<const char *> Argv;
  for (const auto &S : A) Argv.push_back(S.c_str());
  return driver::IsClangCL(driver::getDriverMode(A[0], llvm::ArrayRef<const char *>(Argv).drop_front()));
}

// getMainExecutable に渡す、この実行ファイルの中の関数のアドレス(main のアドレスは C++ では使えない)
void executableAnchor() {}

// 相対パスは、TU のコンパイルのディレクトリ(FileManager の作業ディレクトリ)から解決する
std::string normalize(llvm::StringRef P, const FileManager *FM = nullptr) {
  llvm::SmallString<260> S(P);
  if (FM)
    FM->makeAbsolutePath(S);
  else
    llvm::sys::fs::make_absolute(S);
  llvm::sys::path::remove_dots(S, true);
  llvm::sys::path::native(S, llvm::sys::path::Style::windows_slash);
  return std::string(S);
}

// Roots はルートの別の書き方(渡されたまま・長い名前・8.3 形式の短い名前)。どれかで始まればルート配下
bool underRoot(const std::string &Abs, const std::vector<std::string> &Roots, std::string &Rel) {
  const std::string *Hit = nullptr;
  for (const auto &R : Roots)
    if (Abs.size() > R.size() && llvm::StringRef(Abs).starts_with_insensitive(R) &&
        (Abs[R.size()] == '\\' || Abs[R.size()] == '/' || R.back() == '\\' || R.back() == '/')) {
      Hit = &R;
      break;
    }
  if (!Hit) return false;
  Rel = Abs.substr(Hit->size());
  while (!Rel.empty() && (Rel[0] == '\\' || Rel[0] == '/')) Rel.erase(0, 1);
  for (char &C : Rel) if (C == '\\') C = '/';
  return true;
}

std::string roleOf(index::SymbolRoleSet R) {
  using index::SymbolRole;
  std::string S = (R & (unsigned)SymbolRole::Definition)    ? "def"
                  : (R & (unsigned)SymbolRole::Declaration) ? "decl"
                  : (R & (unsigned)SymbolRole::Call)        ? "call"
                  : (R & (unsigned)SymbolRole::Write)       ? "write"
                                                             : "ref";
  if (R & (unsigned)SymbolRole::Implicit) S += "-implicit";
  return S;
}

// 仮想関数の呼び出しの形(役割の接尾辞)。-dynamic は限定名の無い呼び出し(p->f()・r.f()・クラスの中の f()。実行時に、
// 受け手の動的な型の上書きが動く)、-static は限定名つき(Base::f()・Super::f()。上書きを経ずに、その関数そのものを呼ぶ)。
// 呼び出しの式で決める。clang の Dynamic の印は MemberExpr にしか付かず、テンプレートの中の依存した式
// (impl_->Advance() など。CXXDependentScopeMemberExpr を推測で解決したもの)には付かない(コードベース A の gtest で 17 行)。
// 式が分からない呼び出しは、Dynamic の印だけを見る(付いていなければ、接尾辞なし)
std::string callForm(const Decl *D, index::SymbolRoleSet R, const Expr *E) {
  using index::SymbolRole;
  if (!(R & (unsigned)SymbolRole::Call)) return "";
  const auto *MD = dyn_cast<CXXMethodDecl>(D);
  if (!MD || !MD->isVirtual()) return "";
  if (const auto *ME = dyn_cast_or_null<MemberExpr>(E)) return ME->hasQualifier() ? "-static" : "-dynamic";
  if (const auto *DE = dyn_cast_or_null<CXXDependentScopeMemberExpr>(E)) return DE->getQualifier() ? "-static" : "-dynamic";
  if (const auto *UE = dyn_cast_or_null<UnresolvedMemberExpr>(E)) return UE->getQualifier() ? "-static" : "-dynamic";
  return (R & (unsigned)SymbolRole::Dynamic) ? "-dynamic" : "";
}

// テンプレート引数を含まない修飾名。実体化ごとに別名(Foo<int>::f, Foo<char>::f)にすると、
// 同じ位置の行が実体化の数だけ増えるので、Foo::f にまとめる。
std::string partName(const NamedDecl *ND) {
  if (const auto *R = dyn_cast<CXXRecordDecl>(ND); R && R->isLambda()) return "(lambda)";
  if (isa<CXXConstructorDecl>(ND) || isa<CXXDestructorDecl>(ND)) {
    std::string P = cast<NamedDecl>(cast<Decl>(ND->getDeclContext()))->getName().str();
    return isa<CXXDestructorDecl>(ND) ? "~" + P : P;
  }
  std::string S = ND->getDeclName().getAsString();
  return S.empty() ? "(anonymous)" : S;
}

std::string qname(const Decl *D) {
  const auto *ND = dyn_cast_or_null<NamedDecl>(D);
  if (!ND) return "";
  std::string S = partName(ND);
  for (const DeclContext *C = ND->getDeclContext(); C && !C->isTranslationUnit(); C = C->getParent())
    if (const auto *P = dyn_cast<NamedDecl>(C)) {
      if (const auto *NS = dyn_cast<NamespaceDecl>(P); NS && NS->isInline()) continue;
      S = partName(P) + "::" + S;
    }
  return S;
}

void sanitize(std::string &S) {
  for (char &C : S) if (C == '\t' || C == '\r' || C == '\n') C = ' ';
}

// cl は BOM の無いソースをコードページ(既定は ANSI コードページ。日本語版 Windows なら 932 = Shift_JIS)で読むが、
// clang は UTF-8 として読む。Shift_JIS の 2 バイト目の `\` が行の継続や文字列のエスケープになり、
// 次の行がコメントに飲まれたり TU が失敗したりする。UTF-8 として正しくないソースと UTF-16 のソースは、
// 読む前に UTF-8 に直して clang に渡す。列番号は UTF-8 に直した行のバイト位置になる(ltq も同じ変換で突き合わせる)。
class Transcoder {
public:
  // 変換した内容。変換の要らないファイルは null。ファイルごとに 1 回だけ読む(全スレッドで共有)
  std::shared_ptr<const std::string> get(llvm::vfs::FileSystem &FS, const llvm::Twine &Path, unsigned Cp) {
    llvm::SmallString<260> Abs;
    Path.toVector(Abs);
    FS.makeAbsolute(Abs);
    std::string Key = std::to_string(Cp) + "|" + Abs.str().str();
    {
      std::lock_guard<std::mutex> L(M);
      if (auto It = Cache.find(Key); It != Cache.end()) return It->second;
    }
    std::shared_ptr<const std::string> R;
    std::string From = Abs.str().str();
    bool Replaced = false;
    if (!Over.empty()) {
      if (auto It = Over.find(overKey(Abs)); It != Over.end()) {
        From = It->second;
        Replaced = true;
      }
    }
    if (auto Buf = FS.getBufferForFile(From, -1, false, /*IsVolatile=*/true)) {
      R = convert((*Buf)->getBuffer(), Cp);
      if (!R && Replaced) R = std::make_shared<const std::string>((*Buf)->getBuffer().str());
    }
    std::lock_guard<std::mutex> L(M);
    return Cache.try_emplace(Key, R).first->second;
  }

  // -overlay: 元のパス -> 代わりに読むファイル(「元のパス<TAB>代わりのファイル」の行)
  bool loadOverlay(llvm::StringRef File) {
    auto Buf = llvm::MemoryBuffer::getFile(File);
    if (!Buf) return false;
    llvm::SmallVector<llvm::StringRef, 16> Lines;
    (*Buf)->getBuffer().split(Lines, '\n', -1, false);
    for (llvm::StringRef L : Lines) {
      auto [A, B] = L.rtrim("\r").split('\t');
      if (!A.empty() && !B.empty()) Over[overKey(A)] = B.str();
    }
    return true;
  }
  static std::string overKey(llvm::StringRef P) {
    llvm::SmallString<260> S(P);
    llvm::sys::path::remove_dots(S, /*remove_dot_dot=*/true);
    std::string K = S.str().lower();
    std::replace(K.begin(), K.end(), '\\', '/');
    return K;
  }
  std::map<std::string, std::string> Over;

private:
  static std::shared_ptr<const std::string> convert(llvm::StringRef B, unsigned Cp) {
    std::string Out;
    llvm::ArrayRef<char> Bytes(B.data(), B.size());
    if (llvm::hasUTF16ByteOrderMark(Bytes))
      return llvm::convertUTF16ToUTF8String(Bytes, Out) ? std::make_shared<const std::string>(std::move(Out)) : nullptr;
    if (Cp == 65001 || B.starts_with("\xEF\xBB\xBF")) return nullptr;
    const auto *P = reinterpret_cast<const llvm::UTF8 *>(B.begin());
    if (llvm::isLegalUTF8String(&P, reinterpret_cast<const llvm::UTF8 *>(B.end()))) return nullptr;
    return codepageToUtf8(Cp, B.data(), B.size(), Out) ? std::make_shared<const std::string>(std::move(Out)) : nullptr;
  }

  std::mutex M;
  std::map<std::string, std::shared_ptr<const std::string>> Cache;
};

class TranscodedFile : public llvm::vfs::File {
public:
  TranscodedFile(llvm::vfs::Status S, std::shared_ptr<const std::string> D) : S(std::move(S)), D(std::move(D)) {}
  llvm::ErrorOr<llvm::vfs::Status> status() override { return S; }
  llvm::ErrorOr<std::unique_ptr<llvm::MemoryBuffer>> getBuffer(const llvm::Twine &Name, int64_t, bool, bool) override {
    return llvm::MemoryBuffer::getMemBuffer(*D, Name.str());  // D は Transcoder が持ち続ける
  }
  std::error_code close() override { return {}; }

private:
  llvm::vfs::Status S;
  std::shared_ptr<const std::string> D;
};

// ソースをメモリーにマップせずに読む。LLVM は大きいファイルを mmap で読み、Windows ではマップされている間
// そのファイルに書き込めない(ERROR_USER_MAPPED_FILE、1224)。索引の自動更新は、エージェントやエディターが
// ソースを書き換えている最中にも走るので、読む中身はいつも複写にする(IsVolatile)
class VolatileFile : public llvm::vfs::File {
public:
  explicit VolatileFile(std::unique_ptr<llvm::vfs::File> F) : F(std::move(F)) {}
  llvm::ErrorOr<llvm::vfs::Status> status() override { return F->status(); }
  llvm::ErrorOr<std::string> getName() override { return F->getName(); }
  llvm::ErrorOr<std::unique_ptr<llvm::MemoryBuffer>> getBuffer(const llvm::Twine &Name, int64_t FileSize,
                                                               bool RequiresNullTerminator, bool) override {
    return F->getBuffer(Name, FileSize, RequiresNullTerminator, /*IsVolatile=*/true);
  }
  std::error_code close() override { return F->close(); }

private:
  std::unique_ptr<llvm::vfs::File> F;
};

// 変換したファイルは、大きさも変換後のものを返す(SourceManager が読んだ大きさと突き合わせる)
class TranscodingFS : public llvm::vfs::ProxyFileSystem {
public:
  TranscodingFS(llvm::IntrusiveRefCntPtr<llvm::vfs::FileSystem> Base, unsigned Cp, Transcoder &T)
      : ProxyFileSystem(std::move(Base)), Cp(Cp), T(T) {}

  llvm::ErrorOr<llvm::vfs::Status> status(const llvm::Twine &Path) override {
    auto S = ProxyFileSystem::status(Path);
    if (!S || !S->isRegularFile()) return S;
    if (auto D = T.get(getUnderlyingFS(), Path, Cp)) return llvm::vfs::Status::copyWithNewSize(*S, D->size());
    return S;
  }
  llvm::ErrorOr<std::unique_ptr<llvm::vfs::File>> openFileForRead(const llvm::Twine &Path) override {
    auto F = ProxyFileSystem::openFileForRead(Path);
    if (!F) return F;
    auto D = T.get(getUnderlyingFS(), Path, Cp);
    if (!D) return std::unique_ptr<llvm::vfs::File>(new VolatileFile(std::move(*F)));
    auto S = (*F)->status();
    if (!S) return S.getError();
    auto NewS = llvm::vfs::Status::copyWithNewSize(*S, D->size());  // D を渡す前に(引数の評価順は決まっていない)
    return std::unique_ptr<llvm::vfs::File>(new TranscodedFile(std::move(NewS), std::move(D)));
  }

private:
  unsigned Cp;
  Transcoder &T;
};

// /source-charset:<名前> などのコードページ。UTF-8 なら 65001、指定が無い・分からなければ 0
unsigned charsetCodepage(llvm::StringRef V) {
  V = V.trim();
  unsigned N;
  if (V.consume_front(".")) return V.getAsInteger(10, N) ? 0 : N;
  std::string L = V.lower();
  static const std::pair<const char *, unsigned> Names[] = {
      {"utf-8", 65001},     {"utf8", 65001},       {"shift_jis", 932}, {"shift-jis", 932}, {"sjis", 932},
      {"cp932", 932},       {"windows-31j", 932},  {"euc-jp", 51932},  {"gb2312", 936},    {"gbk", 936},
      {"cp936", 936},       {"big5", 950},         {"cp950", 950},     {"euc-kr", 949},    {"ks_c_5601-1987", 949},
      {"cp949", 949},       {"windows-1252", 1252}, {"cp1252", 1252},  {"iso-8859-1", 28591}};
  for (const auto &[Name, Cp] : Names)
    if (L == Name) return Cp;
  return 0;
}

// コンパイルのコマンドからソースのコードページを決める(cl の /utf-8・/source-charset、clang の -finput-charset)
unsigned sourceCodepage(const std::vector<std::string> &Args, unsigned Default) {
  unsigned Cp = Default;
  for (llvm::StringRef A : Args) {
    if (A == "/utf-8" || A == "-utf-8") Cp = 65001;
    else if ((A.consume_front("/source-charset:") || A.consume_front("-source-charset:") ||
              A.consume_front("-finput-charset=")))
      if (unsigned C = charsetCodepage(A)) Cp = C;
  }
  return Cp;
}

bool isCharsetArg(llvm::StringRef A) {
  return A.starts_with("/source-charset:") || A.starts_with("-source-charset:") || A.starts_with("/execution-charset:") ||
         A.starts_with("-execution-charset:") || A.starts_with("-finput-charset=") || A.starts_with("-fexec-charset=");
}

// ルート配下の位置だけを TSV の行にする。索引と依存呼び出しの両方が使う。
class RowWriter {
public:
  RowWriter(const std::vector<std::string> &R, std::vector<std::string> &Rows, std::vector<std::string> &Skips)
      : Roots(R), Rows(Rows), Skips(Skips) {}

  void add(const SourceManager &SM, SourceLocation Loc, std::string Sym, const std::string &Role,
           std::string Caller) {
    SourceLocation FL = SM.getFileLoc(Loc);
    FileID FID = SM.getFileID(FL);
    const std::string &Path = relPath(SM, FID);
    if (Path.empty() || Sym.empty()) return;

    unsigned Line = SM.getSpellingLineNumber(FL), Col = SM.getSpellingColumnNumber(FL);
    llvm::StringRef Buf = SM.getBufferData(FID);
    unsigned Off = SM.getFileOffset(FL);
    size_t B = Buf.rfind('\n', Off);  // [0, Off) を探す。列 1 のトークンでも直前の改行を見つける
    B = B == llvm::StringRef::npos ? 0 : B + 1;
    size_t E = Buf.find('\n', Off);
    std::string Src = Buf.slice(B, E == llvm::StringRef::npos ? Buf.size() : E).trim().str();
    if (Src.size() > 200) Src.resize(200);

    sanitize(Sym);
    sanitize(Caller);
    sanitize(Src);
    Rows.push_back(Sym + "\t" + Role + "\t" + Caller + "\t" + Path + ":" +
                   std::to_string(Line) + ":" + std::to_string(Col) + "\t" + Src);
  }

  // コメント(comment)と無効な #if(inactive)の範囲。path <TAB> 種類 <TAB> line:col <TAB> line:col
  // 名前のトークン 1 つの範囲。名前がファイルに書かれた位置そのもののときだけ(マクロの本体から来た名前は、使った行に無い)
  void addName(const SourceManager &SM, SourceLocation Loc, unsigned Len, const char *Kind) {
    SourceLocation FL = SM.getFileLoc(Loc);
    if (FL.isInvalid() || FL != SM.getSpellingLoc(Loc)) return;
    addRange(SM, SourceRange(FL, FL.getLocWithOffset(Len)), Kind);
  }

  void addRange(const SourceManager &SM, SourceRange R, const char *Kind) {
    SourceLocation B = SM.getFileLoc(R.getBegin()), E = SM.getFileLoc(R.getEnd());
    FileID FID = SM.getFileID(B);
    if (B.isInvalid() || E.isInvalid() || FID != SM.getFileID(E)) return;
    const std::string &Path = relPath(SM, FID);
    if (Path.empty()) return;
    Skips.push_back(Path + "\t" + Kind + "\t" + std::to_string(SM.getSpellingLineNumber(B)) + ":" +
                    std::to_string(SM.getSpellingColumnNumber(B)) + "\t" +
                    std::to_string(SM.getSpellingLineNumber(E)) + ":" +
                    std::to_string(SM.getSpellingColumnNumber(E)));
  }

  bool inRoot(const SourceManager &SM, FileID FID) { return !relPath(SM, FID).empty(); }

  size_t skipCount() const { return Skips.size(); }
  void eraseSkips(std::vector<size_t> Index) {
    std::sort(Index.begin(), Index.end(), std::greater<size_t>());
    Index.erase(std::unique(Index.begin(), Index.end()), Index.end());
    for (size_t I : Index) Skips.erase(Skips.begin() + I);
  }

private:
  // ルート配下ならルートからの相対パス、それ以外は空
  const std::string &relPath(const SourceManager &SM, FileID FID) {
    auto It = PathCache.find(FID);
    if (It == PathCache.end()) {
      std::string Rel;
      OptionalFileEntryRef FE = SM.getFileEntryRefForID(FID);
      It = PathCache.try_emplace(FID, FE && underRoot(normalize(FE->getName(), &SM.getFileManager()), Roots, Rel) ? Rel : "").first;
    }
    return It->second;
  }

  std::vector<std::string> Roots;
  std::vector<std::string> &Rows, &Skips;
  llvm::DenseMap<FileID, std::string> PathCache;
};

// 無効な #if の範囲。同じ TU が同じファイルを 2 回以上読んだとき(インクルードガードが clang の最適化に乗らず、
// 2 回目に本体がまるごと飛ぶ場合や、マクロを変えて読み直す .inc など)は、どの回でも無効だった部分だけを残す。
// そうしないと、1 回目に解析された行まで「無効」になる
class SkippedRanges : public PPCallbacks {
public:
  SkippedRanges(const SourceManager &SM, RowWriter &W) : SM(SM), W(W) {}

  void FileChanged(SourceLocation Loc, FileChangeReason Reason, SrcMgr::CharacteristicKind, FileID) override {
    if (Reason != EnterFile) return;
    FileID FID = SM.getFileID(Loc);
    if (OptionalFileEntryRef FE = SM.getFileEntryRefForID(FID)) {
      auto &V = Entered[&FE->getFileEntry()];
      if (std::find(V.begin(), V.end(), FID) == V.end()) V.push_back(FID);
    }
  }
  void SourceRangeSkipped(SourceRange R, SourceLocation) override {
    size_t N = W.skipCount();
    W.addRange(SM, R, "inactive");
    if (W.skipCount() == N) return;  // ルートの外
    SourceLocation B = SM.getFileLoc(R.getBegin()), E = SM.getFileLoc(R.getEnd());
    Ranges[SM.getFileID(B)].push_back({SM.getFileOffset(B), SM.getFileOffset(E), N});
  }

  void flush() {
    using Span = std::pair<unsigned, unsigned>;
    std::vector<size_t> Drop;
    std::vector<std::pair<FileID, Span>> Add;
    for (auto &[FE, FIDs] : Entered) {
      if (FIDs.size() < 2) continue;
      std::vector<Span> Cur;
      bool First = true, Any = false;
      for (FileID F : FIDs) {
        std::vector<Span> V;
        if (auto It = Ranges.find(F); It != Ranges.end()) {
          Any = true;
          for (const auto &R : It->second) V.push_back({R.B, R.E});
        }
        std::sort(V.begin(), V.end());
        if (First) {
          Cur = std::move(V);
        } else {  // 2 つの並びの共通部分(1 回の読み込みの中では、範囲は重ならない)
          std::vector<Span> X;
          for (size_t I = 0, J = 0; I < Cur.size() && J < V.size();) {
            unsigned B = std::max(Cur[I].first, V[J].first), E = std::min(Cur[I].second, V[J].second);
            if (B < E) X.push_back({B, E});
            (Cur[I].second < V[J].second ? I : J)++;
          }
          Cur = std::move(X);
        }
        First = false;
      }
      if (!Any) continue;
      // 範囲は「#if の行から、それを終える #else・#endif の行まで」なので、隣り合う分岐は境目の行だけ重なる。本体の無い重なりは捨てる
      Cur.erase(std::remove_if(Cur.begin(), Cur.end(), [&](const Span &S) {
                  return SM.getLineNumber(FIDs[0], S.first) == SM.getLineNumber(FIDs[0], S.second);
                }), Cur.end());
      std::set<Span> Keep(Cur.begin(), Cur.end()), Have;
      for (FileID F : FIDs)
        if (auto It = Ranges.find(F); It != Ranges.end())
          for (const auto &R : It->second) {
            if (Keep.count({R.B, R.E})) Have.insert({R.B, R.E});
            else Drop.push_back(R.Index);
          }
      for (const Span &S : Cur)
        if (!Have.count(S)) Add.push_back({FIDs[0], S});
    }
    W.eraseSkips(Drop);
    for (const auto &[F, S] : Add) {
      SourceLocation Start = SM.getLocForStartOfFile(F);
      W.addRange(SM, SourceRange(Start.getLocWithOffset(S.first), Start.getLocWithOffset(S.second)), "inactive");
    }
    Entered.clear();
    Ranges.clear();
  }

private:
  struct Skipped {
    unsigned B, E;
    size_t Index;  // RowWriter の Skips の中の位置
  };
  const SourceManager &SM;
  RowWriter &W;
  std::map<const FileEntry *, std::vector<FileID>> Entered;  // ファイル -> 読み込みごとの FileID
  llvm::DenseMap<FileID, std::vector<Skipped>> Ranges;
};

// マクロそのものの行: #define(macro)・展開(expand)・#ifdef / defined()(ifdef)・#undef(undef)。記号はマクロの名前。
// 展開は、名前がファイルに書かれているもの(マクロの引数に書かれたものを含む)だけ。別のマクロの本体の中の展開は、
// 使った行に名前が無いので出さない(本体の行はテキスト検索が「マクロ定義の中」と分類する)。
// あわせて、ファイルに書かれた関数形式のマクロの引数のうち、展開後のトークンに残らなかった名前の範囲を
// dropped として出す(無効にした assert やログの引数、# で文字列にされた・## で貼り合わされた名前)。
class MacroRefs : public PPCallbacks {
public:
  MacroRefs(Preprocessor &PP, RowWriter &W) : PP(PP), SM(PP.getSourceManager()), W(W) {}

  void MacroDefined(const Token &Name, const MacroDirective *MD) override {
    add(Name, "macro", renameOf(MD ? MD->getMacroInfo() : nullptr));
  }
  void MacroUndefined(const Token &Name, const MacroDefinition &, const MacroDirective *) override { add(Name, "undef"); }
  void Defined(const Token &Name, const MacroDefinition &, SourceRange) override { add(Name, "ifdef"); }
  void Ifdef(SourceLocation, const Token &Name, const MacroDefinition &) override { add(Name, "ifdef"); }
  void Ifndef(SourceLocation, const Token &Name, const MacroDefinition &) override { add(Name, "ifdef"); }
  void Elifdef(SourceLocation, const Token &Name, const MacroDefinition &) override { add(Name, "ifdef"); }
  void Elifndef(SourceLocation, const Token &Name, const MacroDefinition &) override { add(Name, "ifdef"); }

  void MacroExpands(const Token &Name, const MacroDefinition &MD, SourceRange Range, const MacroArgs *Args) override {
    add(Name, "expand", renameOf(MD.getMacroInfo()));
    // 引数に書かれたマクロの名前は、展開されると別のトークンになるので、展開されたこと自体を「引数が使われた」の印にする
    // (WRAP(ONE) の ONE は残り、引数を捨てる LOG(ERR_CODE) の ERR_CODE は展開されないので捨てられたまま)
    if (!Pending.empty()) {
      SourceLocation NL = Name.getLocation(), FL = SM.getFileLoc(NL);
      if (FL == SM.getSpellingLoc(NL)) {
        std::pair<FileID, unsigned> D = SM.getDecomposedLoc(FL);
        if (Files.count(D.first)) Survived.insert(key(D.first, D.second));
      }
    }
    // 引数の行き先を見るのは、ファイルに書かれた一番外の呼び出しだけ(中の呼び出しの引数は、外の引数のトークンでもある)
    if (!Args || !Range.getBegin().isFileID() || PP.isParsingIfOrElifDirective() || inDirective(Range.getBegin())) return;
    FileID F = SM.getFileID(Range.getBegin());
    if (!W.inRoot(SM, F)) return;
    for (unsigned I = 0, N = Args->getNumMacroArguments(); I < N; ++I) {
      Arg A{F, {}};
      for (const Token *T = Args->getUnexpArgument(I); T->isNot(tok::eof); ++T)
        if (T->is(tok::identifier) && T->getLocation().isFileID())
          A.Toks.push_back({SM.getFileOffset(T->getLocation()), T->getLength()});
      if (!A.Toks.empty()) {
        Files.insert(F);
        Pending.push_back(std::move(A));
      }
    }
  }

  // その位置の行が指令(# で始まる行。#include MACRO(x)・#pragma など)か
  bool inDirective(SourceLocation L) const {
    bool Bad = false;
    llvm::StringRef Buf = SM.getBufferData(SM.getFileID(L), &Bad);
    if (Bad) return false;
    unsigned Off = SM.getFileOffset(L);
    size_t B = Buf.rfind('\n', Off);
    B = B == llvm::StringRef::npos ? 0 : B + 1;
    while (B > 0) {  // 前の行が `\` で終わっていれば、その行の続き
      size_t P = Buf.rfind('\n', B - 1);
      P = P == llvm::StringRef::npos ? 0 : P + 1;
      if (!Buf.slice(P, B - 1).rtrim("\r").ends_with("\\")) break;
      B = P;
    }
    return Buf.substr(B, Off - B).ltrim(" \t").starts_with("#");
  }

  // 構文解析に渡ったトークンのうち、マクロの引数から来たものの元の位置を覚える(Preprocessor::setTokenWatcher から呼ぶ)
  void onToken(const Token &T) {
    SourceLocation L = T.getLocation();
    if (Pending.empty() || !L.isMacroID() || !SM.isMacroArgExpansion(L)) return;
    SourceLocation F = SM.getFileLoc(L);
    if (F != SM.getSpellingLoc(L)) return;
    std::pair<FileID, unsigned> D = SM.getDecomposedLoc(F);
    if (Files.count(D.first)) Survived.insert(key(D.first, D.second));
  }

  // TU の終わりに、残らなかった名前を、引数ごとに続いている範囲にまとめて出す
  void flush() {
    for (const Arg &A : Pending) {
      size_t I = 0;
      while (I < A.Toks.size()) {
        if (Survived.count(key(A.F, A.Toks[I].first))) { ++I; continue; }
        size_t J = I;
        while (J + 1 < A.Toks.size() && !Survived.count(key(A.F, A.Toks[J + 1].first))) ++J;
        SourceLocation B = SM.getComposedLoc(A.F, A.Toks[I].first);
        SourceLocation E = SM.getComposedLoc(A.F, A.Toks[J].first + A.Toks[J].second);
        W.addRange(SM, SourceRange(B, E), "dropped");
        I = J + 1;
      }
    }
    Pending.clear();
    Survived.clear();
    Files.clear();
  }

private:
  struct Arg {
    FileID F;
    std::vector<std::pair<unsigned, unsigned>> Toks;  // 名前のトークンの (ファイル内の位置, 長さ)
  };
  static uint64_t key(FileID F, unsigned Off) { return (uint64_t)(unsigned)F.getHashValue() << 32 | Off; }
  // 名前 1 つに置き換えるオブジェクト形式のマクロ(windows.h の #define GetMessage GetMessageW)の、置き換え後の名前
  // 名前 1 つに置き換えるオブジェクト形式のマクロ、または本体が `別の名前(` で始まる関数形式のマクロ(`#define Foo(x) Foo_impl(x)`)の、
  // 置き換え後の名前。置き換え後の名前もマクロなら辿る(windows.h の CreateWindow -> CreateWindowW -> CreateWindowExW)。
  // 関数形式は、置き換え後の名前を呼ぶもの(`Logger::Error(m)` のように :: で始まらない)だけ
  std::string renameOf(const MacroInfo *MI) const {
    std::string To;
    for (int Hop = 0; MI && Hop < 4; ++Hop) {
      unsigned N = MI->getNumTokens();
      if (N == 0) break;
      const Token &T = MI->getReplacementToken(0);
      if (!T.is(tok::identifier) || !T.getIdentifierInfo() || MI->getParameterNum(T.getIdentifierInfo()) != -1) break;
      if (MI->isObjectLike() ? N != 1 : !(N >= 2 && MI->getReplacementToken(1).is(tok::l_paren))) break;
      To = T.getIdentifierInfo()->getName().str();
      MI = PP.getMacroInfo(T.getIdentifierInfo());
    }
    return To;
  }
  void add(const Token &Name, const char *Role, const std::string &To = "") {
    SourceLocation L = Name.getLocation();
    const IdentifierInfo *II = Name.getIdentifierInfo();
    // 名前がファイルに書かれた位置そのものだけ(引数として渡されたマクロの名前でも、書かれた場所が別のマクロの本体なら出さない。
    // gtest の EXPECT_EQ の本体が GTEST_NONFATAL_FAILURE_ を内側のマクロの引数に渡すので、EXPECT_EQ の行に載ってしまう)
    if (!II || L.isInvalid() || SM.getFileLoc(L) != SM.getSpellingLoc(L)) return;
    W.add(SM, L, II->getName().str(), Role, To);
  }

  Preprocessor &PP;
  const SourceManager &SM;
  RowWriter &W;
  std::vector<Arg> Pending;
  llvm::DenseSet<uint64_t> Survived;
  llvm::DenseSet<FileID> Files;  // Pending の引数が書かれたファイル
};

class Comments : public CommentHandler {
public:
  explicit Comments(RowWriter &W) : W(W) {}
  bool HandleComment(Preprocessor &PP, SourceRange R) override {
    W.addRange(PP.getSourceManager(), R, "comment");
    return false;
  }

private:
  RowWriter &W;
};

class Consumer : public index::IndexDataConsumer {
public:
  explicit Consumer(RowWriter &W) : W(W) {}
  void initialize(ASTContext &C) override { Ctx = &C; }

  bool handleDeclOccurrence(const Decl *D, index::SymbolRoleSet Roles,
                            llvm::ArrayRef<index::SymbolRelation> Rels, SourceLocation Loc,
                            ASTNodeInfo Info) override {
    if (isa<NamespaceDecl>(D) || isa<NamespaceAliasDecl>(D)) return true;  // 雑音なので出さない
    // 関数の中の変数・引数と、テンプレートの引数は行にせず、名前の位置だけを local の範囲にする。テキスト検索で同じ名前が
    // 当たったとき(ローカル変数 begin と std::vector::begin など)、ltq が「別の記号」と言えるように(コードベース A で、理由の付かない
    // 一致の大半がこれだった)。.skip.tsv は大きくなる(O3DE で 28 MB -> 139 MB)。要らなければ -no-local。
    // 同じ名前の記号が TU に出てくるものだけに絞ると、ヘッダーの範囲が TU ごとに変わって .own.bin が大きくなり、
    // 合計ではかえって大きかった(コードベース A で .skip.tsv 6.9 -> 5.6 MB、.own.bin 1.2 -> 3.6 MB)
    if (index::isFunctionLocalSymbol(D) || isa<TemplateTypeParmDecl, NonTypeTemplateParmDecl, TemplateTemplateParmDecl>(D)) {
      if (const auto *ND = dyn_cast<NamedDecl>(D); ND && ND->getIdentifier() && !(Roles & (unsigned)index::SymbolRole::Implicit))
        W.addName(Ctx->getSourceManager(), Loc, ND->getName().size(), "local");
      return true;
    }
    std::string Caller;
    for (const auto &Rel : Rels)
      if (Rel.Roles & (unsigned)index::SymbolRole::RelationCalledBy) { Caller = callerName(Rel.RelatedSymbol); break; }
    if (Caller.empty())
      for (const auto &Rel : Rels)
        if (Rel.Roles & (unsigned)index::SymbolRole::RelationContainedBy) { Caller = callerName(Rel.RelatedSymbol); break; }
    // 仮想関数の呼び出しには形を付ける。印の無い呼び出しの行は前の ltidx の行なので、ltq は行の文面で形を決める
    std::string Role = roleOf(Roles) + callForm(D, Roles, Info.OrigE);
    W.add(Ctx->getSourceManager(), Loc, qname(D), Role, Caller);
    // 仮想関数の上書き。基底のポインタ越しの呼び出しは基底のメソッドに解決されるので、ltq が上書きの関係から辿る
    for (const auto &Rel : Rels)
      if (Rel.Roles & (unsigned)index::SymbolRole::RelationOverrideOf)
        W.add(Ctx->getSourceManager(), Loc, qname(D), "override", qname(Rel.RelatedSymbol));
    // テンプレート内の `using Base::f;` 越しの参照は、宣言の先が実体化でしか分からないので TU の終わりに引く
    if (const auto *U = dyn_cast<UnresolvedUsingValueDecl>(D); U && !(Roles & (unsigned)index::SymbolRole::Declaration))
      Pending.push_back({U, Loc, Role, Caller});
    return true;
  }

  // 呼び出し元の名前。関数の中の変数(static constexpr の定数・gtest の一時変数など)に含まれる参照は、それを囲む関数にする
  // (関数の中の記号は索引に載らないので、呼び出し元に名前を出しても引けない)
  static std::string callerName(const Decl *R) {
    while (R && index::isFunctionLocalSymbol(R) && !isa<FunctionDecl>(R)) {
      const DeclContext *DC = R->getParentFunctionOrMethod();
      R = DC ? dyn_cast<Decl>(DC) : nullptr;
    }
    return R ? qname(R) : std::string();
  }

  struct Using {
    const UnresolvedUsingValueDecl *U;
    SourceLocation Loc;
    std::string Role, Caller;
  };
  std::vector<Using> Pending;


private:
  RowWriter &W;
  ASTContext *Ctx = nullptr;
};

// `using Base::f;` を持つクラステンプレートの実体化を見て、f の宣言の先(UsingShadowDecl の先)を集める。
// 実体化が TU に無い・部分特殊化の中にある場合は空になり、元の行(I::f)だけが残る。
std::vector<std::string> usingTargets(const UnresolvedUsingValueDecl *U) {
  std::set<std::string> R;
  const auto *RD = dyn_cast<CXXRecordDecl>(U->getDeclContext());
  const ClassTemplateDecl *CT = RD ? RD->getDescribedClassTemplate() : nullptr;
  if (!CT) return {};
  for (const ClassTemplateSpecializationDecl *S : CT->specializations()) {
    if (!S->isCompleteDefinition()) continue;
    for (const NamedDecl *N : S->lookup(U->getDeclName()))
      if (const auto *Sh = dyn_cast<UsingShadowDecl>(N)) {
        const NamedDecl *T = Sh->getTargetDecl();
        if (const auto *FT = dyn_cast<FunctionTemplateDecl>(T)) T = FT->getTemplatedDecl();
        R.insert(qname(T));
      }
  }
  return {R.begin(), R.end()};
}

class UsingResolver : public ASTConsumer {
public:
  UsingResolver(std::shared_ptr<Consumer> C, RowWriter &W) : C(std::move(C)), W(W) {}
  void HandleTranslationUnit(ASTContext &Ctx) override {
    llvm::DenseMap<const Decl *, std::vector<std::string>> Cache;
    for (const auto &P : C->Pending) {
      auto It = Cache.find(P.U);
      if (It == Cache.end()) It = Cache.try_emplace(P.U, usingTargets(P.U)).first;
      for (const auto &T : It->second) W.add(Ctx.getSourceManager(), P.Loc, T, P.Role + "-using", P.Caller);
    }
  }

private:
  std::shared_ptr<Consumer> C;
  RowWriter &W;
};

// テンプレートの中で解決先が決まらない呼び出し・参照(t.f(), this->f(), Base::f(), f(t) の ADL)。
// 実体化されないテンプレートのものは clang::index に現れないので、名前だけの行 `?::f <TAB> dep` にする。
// RecursiveASTVisitor は既定で実体化を辿らないので、見るのはテンプレートの本体だけになる。
class DepVisitor : public RecursiveASTVisitor<DepVisitor> {
public:
  DepVisitor(ASTContext &C, RowWriter &W) : SM(C.getSourceManager()), W(W) {}

  bool TraverseFunctionDecl(FunctionDecl *F) { return withCaller(F, [&] { return RecursiveASTVisitor::TraverseFunctionDecl(F); }); }
  bool TraverseCXXMethodDecl(CXXMethodDecl *F) { return withCaller(F, [&] { return RecursiveASTVisitor::TraverseCXXMethodDecl(F); }); }
  bool TraverseCXXConstructorDecl(CXXConstructorDecl *F) { return withCaller(F, [&] { return RecursiveASTVisitor::TraverseCXXConstructorDecl(F); }); }
  bool TraverseCXXDestructorDecl(CXXDestructorDecl *F) { return withCaller(F, [&] { return RecursiveASTVisitor::TraverseCXXDestructorDecl(F); }); }

  bool VisitCXXDependentScopeMemberExpr(CXXDependentScopeMemberExpr *E) { return emit(E->getMemberLoc(), E->getMember()); }
  bool VisitDependentScopeDeclRefExpr(DependentScopeDeclRefExpr *E) { return emit(E->getLocation(), E->getDeclName()); }
  bool VisitUnresolvedMemberExpr(UnresolvedMemberExpr *E) { return emit(E->getMemberLoc(), E->getMemberName()); }
  bool VisitUnresolvedLookupExpr(UnresolvedLookupExpr *E) { return emit(E->getNameLoc(), E->getName()); }

private:
  template <class Fn> bool withCaller(FunctionDecl *F, Fn &&Body) {
    std::string Saved = Caller;
    Caller = qname(F);
    bool R = Body();
    Caller = Saved;
    return R;
  }
  bool emit(SourceLocation Loc, DeclarationName N) {
    if (Loc.isInvalid() || SM.isInSystemHeader(Loc) || N.isEmpty()) return true;
    W.add(SM, Loc, "?::" + N.getAsString(), "dep", Caller);
    return true;
  }

  const SourceManager &SM;
  RowWriter &W;
  std::string Caller;
};

class DepConsumer : public ASTConsumer {
public:
  explicit DepConsumer(RowWriter &W) : W(W) {}
  void HandleTranslationUnit(ASTContext &C) override { DepVisitor(C, W).TraverseDecl(C.getTranslationUnitDecl()); }

private:
  RowWriter &W;
};

struct Output {
  std::vector<std::string> Rows, Skips, Deps;  // Deps: TU が読んだルート配下のファイル(ルートからの相対パス)
};

class IndexAction : public ASTFrontendAction {
public:
  IndexAction(const std::vector<std::string> &R, Output &O) : Roots(R), O(O), W(R, O.Rows, O.Skips), C(W) {}

  bool BeginSourceFileAction(CompilerInstance &CI) override {
    Preprocessor &PP = CI.getPreprocessor();
    if (KeepGoing) {  // 仮の解析では、無効だった分岐の #include が見つからないことがある。そこで止めない
      CI.getDiagnostics().setFatalsAsError(true);
      CI.getDiagnostics().setErrorLimit(0);
    }
    auto S = std::make_unique<SkippedRanges>(CI.getSourceManager(), W);
    Skipped = S.get();
    PP.addPPCallbacks(std::move(S));
    auto M = std::make_unique<MacroRefs>(PP, W);
    Macros = M.get();
    PP.addPPCallbacks(std::move(M));
    PP.setTokenWatcher([this](const Token &T) { Macros->onToken(T); });
    PP.addCommentHandler(&C);
    return true;
  }
  void EndSourceFileAction() override {
    CompilerInstance &CI = getCompilerInstance();
    CI.getPreprocessor().removeCommentHandler(&C);
    CI.getPreprocessor().setTokenWatcher(nullptr);
    if (Macros) Macros->flush();
    Macros = nullptr;
    if (Skipped) Skipped->flush();
    Skipped = nullptr;
    const SourceManager &SM = CI.getSourceManager();
    for (auto It = SM.fileinfo_begin(); It != SM.fileinfo_end(); ++It) {
      std::string Rel;
      if (underRoot(normalize(It->first.getName(), &SM.getFileManager()), Roots, Rel)) O.Deps.push_back(Rel);
    }
  }

  std::unique_ptr<ASTConsumer> CreateASTConsumer(CompilerInstance &CI, llvm::StringRef) override {
    index::IndexingOptions O;
    O.SystemSymbolFilter = index::IndexingOptions::SystemSymbolFilterKind::None;
    O.IndexFunctionLocals = !NoLocal;  // 行にはせず、local の範囲だけを出す(Consumer)
    O.IndexParametersInDeclarations = true;
    O.IndexTemplateParameters = !NoLocal;
    O.IndexImplicitInstantiation = !NoInst;  // DeferIndexingToEndOfTranslationUnit は指定しない
    std::vector<std::unique_ptr<ASTConsumer>> Cs;
    auto C = std::make_shared<Consumer>(W);
    Cs.push_back(index::createIndexingASTConsumer(C, O, CI.getPreprocessorPtr()));
    Cs.push_back(std::make_unique<UsingResolver>(C, W));  // 索引の後に走る(MultiplexConsumer は順に呼ぶ)
    if (!NoDep) Cs.push_back(std::make_unique<DepConsumer>(W));
    return std::make_unique<MultiplexConsumer>(std::move(Cs));
  }

private:
  std::vector<std::string> Roots;
  Output &O;
  RowWriter W;
  Comments C;
  MacroRefs *Macros = nullptr;  // Preprocessor が持つ
  SkippedRanges *Skipped = nullptr;  // 同上
};

class Factory : public tooling::FrontendActionFactory {
public:
  Factory(const std::vector<std::string> &R, Output &O) : Roots(R), O(O) {}
  std::unique_ptr<FrontendAction> create() override { return std::make_unique<IndexAction>(Roots, O); }

private:
  std::vector<std::string> Roots;
  Output &O;
};

class ErrorCounter : public DiagnosticConsumer {
public:
  unsigned Errors = 0, Fatal = 0;
  std::string First;
  void HandleDiagnostic(DiagnosticsEngine::Level L, const Diagnostic &Info) override {
    if (L < DiagnosticsEngine::Error) return;
    ++Errors;
    if (L == DiagnosticsEngine::Fatal) ++Fatal;
    if (First.empty()) {
      llvm::SmallString<200> Msg;
      Info.FormatDiagnostic(Msg);
      First = Msg.str().str();
      sanitize(First);
    }
  }
};

// パスの比較用のキー(Windows では大文字小文字を区別しない)
std::string pathKey(llvm::StringRef P) { return llvm::StringRef(normalize(P)).lower(); }

// TU のコンパイルのコマンド(ディレクトリと引数)と、索引の中身を変える ltidx のオプションのハッシュ。
// -update で、コマンドの変わった TU を見つける。オプションを変えて -update すると、全 TU を解析し直す
// 出力の形式の版。行の種類や範囲の種類を足したら上げる(-update が、前の形式で作った TU を解析し直すように)
// 2: マクロの行(macro・expand・ifdef・undef)と、dropped・local の範囲
static const char *const FormatVersion = "2";

std::string commandHash(const tooling::CompilationDatabase &Cdb, const std::string &F) {
  std::string S = std::string(FormatVersion) + '\x1f' + pathKey(Root) + '\x1f' + ResourceDir + '\x1f' + std::to_string(SourceCp) + '\x1f' + (NoInst ? "i" : "") +
                  (Delayed ? "d" : "") + (NoDep ? "n" : "") + (NoLocal ? "l" : "") + '\x1e';
  for (const auto &C : Cdb.getCompileCommands(F)) {
    S += C.Directory + '\x1f';
    for (const auto &A : C.CommandLine) S += A + '\x1f';
    S += '\x1e';
  }
  return llvm::utohexstr(llvm::xxh3_64bits(llvm::arrayRefFromStringRef<uint8_t>(S)), true);
}

// 空行を除いた行。SkipHeader なら 1 行目(見出し)も除く
void readLines(const std::string &P, std::vector<std::string> &Out, bool SkipHeader) {
  auto B = llvm::MemoryBuffer::getFile(P);
  if (!B) return;
  llvm::StringRef S = (*B)->getBuffer();
  for (bool First = true; !S.empty(); First = false) {
    auto [L, Rest] = S.split('\n');
    S = Rest;
    if (!(First && SkipHeader) && !L.empty()) Out.push_back(L.str());
  }
}

// 索引の行の 4 列目(path:line:col)のパス
llvm::StringRef rowPath(llvm::StringRef Row) {
  for (int I = 0; I < 3; ++I) Row = Row.split('\t').second;
  return Row.split('\t').first.rsplit(':').first.rsplit(':').first;
}

uint64_t lineHash(llvm::StringRef L) { return llvm::xxh3_64bits(llvm::arrayRefFromStringRef<uint8_t>(L)); }

// ファイル(ルートからの相対パス)-> 行(索引と .skip.tsv の行)のハッシュ。整列済み・重複なし
using FileHashes = std::map<std::string, std::vector<uint64_t>>;

void sortUnique(std::vector<uint64_t> &V) {
  std::sort(V.begin(), V.end());
  V.erase(std::unique(V.begin(), V.end()), V.end());
}

// 行の出どころ(<out>.own.bin)。複数の TU が読むファイル F について、F を読む TU すべてが出した行を「共通」とし、
// TU ごとに共通でない行(実体化・マクロで TU ごとに変わる行)のハッシュだけを持つ。
// F の行は、どの TU の「共通でない行」にも無ければ、F を読む TU すべてが出したものと分かる。
//   "LTOWN001" | u64 同じ回の .tu.tsv のハッシュ | u8 正確か | u32 ファイル数 {u32 長さ, パス}
//   | u32 TU 数 {u32 長さ, TU のパス, u32 ファイルの数 {u32 ファイルの番号, u32 行数, u64 ハッシュ...}}
// 正確でない(0)のは、出どころの無い前回の索引から -update で作ったもの。共通の行に、消すべき古い行が混ざりうる
const char OwnMagic[] = "LTOWN001";

struct OwnFile {
  bool Exact = false;
  std::map<std::string, FileHashes> Tus;  // pathKey(TU) -> 共通でない行
};

uint64_t fileHash(const std::string &P) {
  auto B = llvm::MemoryBuffer::getFile(P);
  return B ? llvm::xxh3_64bits(llvm::arrayRefFromStringRef<uint8_t>((*B)->getBuffer())) : 0;
}

// 読めない・壊れている・別の回の索引のものなら、理由を返す
std::string readOwn(const std::string &P, uint64_t TuHash, OwnFile &O) {
  auto B = llvm::MemoryBuffer::getFile(P);
  if (!B) return "無い";
  llvm::StringRef S = (*B)->getBuffer();
  bool Bad = false;
  auto take = [&](size_t N) {
    if (Bad || S.size() < N) { Bad = true; return llvm::StringRef(); }
    llvm::StringRef R = S.take_front(N);
    S = S.drop_front(N);
    return R;
  };
  auto u32 = [&] { llvm::StringRef R = take(4); return Bad ? 0u : llvm::support::endian::read32le(R.data()); };
  auto u64 = [&] { llvm::StringRef R = take(8); return Bad ? uint64_t(0) : llvm::support::endian::read64le(R.data()); };
  if (take(8) != OwnMagic) return "形式が違う";
  if (u64() != TuHash) return ".tu.tsv と組でない(別の回の索引のもの)";
  O.Exact = take(1) == "\x01";
  uint32_t NF = u32();
  if (NF > S.size() / 4) return "壊れている";
  std::vector<std::string> Files(NF);
  for (auto &F : Files) F = take(u32()).str();
  for (uint32_t T = u32(); T-- && !Bad;) {
    FileHashes &H = O.Tus[pathKey(take(u32()))];
    for (uint32_t G = u32(); G-- && !Bad;) {
      uint32_t F = u32();
      if (F >= Files.size()) Bad = true;
      std::vector<uint64_t> &V = H[Bad ? "" : Files[F]];
      for (uint32_t N = u32(); N-- && !Bad;) V.push_back(u64());
    }
  }
  if (Bad || !S.empty()) return "壊れている";
  return "";
}

// Tus: TU のパスと、共通でない行
bool writeOwn(const std::string &P, uint64_t TuHash, bool Exact,
              const std::vector<std::pair<std::string, const FileHashes *>> &Tus) {
  std::map<std::string, uint32_t> Ids;
  for (const auto &[T, H] : Tus)
    for (const auto &[F, V] : *H) Ids.try_emplace(F, 0);
  std::vector<const std::string *> Names;
  for (auto &[F, Id] : Ids) { Id = Names.size(); Names.push_back(&F); }
  std::error_code EC;
  llvm::raw_fd_ostream OS(P, EC);
  if (EC) { llvm::errs() << P << ": " << EC.message() << "\n"; return false; }
  llvm::support::endian::Writer W(OS, llvm::endianness::little);
  auto str = [&](llvm::StringRef X) { W.write<uint32_t>(X.size()); OS << X; };
  OS << OwnMagic;
  W.write<uint64_t>(TuHash);
  OS << (Exact ? '\x01' : '\x00');
  W.write<uint32_t>(Names.size());
  for (const auto *N : Names) str(*N);
  W.write<uint32_t>(Tus.size());
  for (const auto &[T, H] : Tus) {
    str(T);
    W.write<uint32_t>(H->size());
    for (const auto &[F, V] : *H) {
      W.write<uint32_t>(Ids[F]);
      W.write<uint32_t>(V.size());
      for (uint64_t X : V) W.write<uint64_t>(X);
    }
  }
  OS.close();
  if (OS.has_error()) { llvm::errs() << "write failed: " << P << ": " << OS.error().message() << "\n"; return false; }
  return true;
}

// 前回の索引のうち、-update で残すもの
struct KeptTu {
  std::string Path, Line;  // Line: .tu.tsv の行
  std::vector<std::string> Deps;
  FileHashes Extra;  // 前回の共通でない行
};
struct Kept {
  std::vector<std::string> Rows, Skips;
  std::vector<KeptTu> Tus;
  FileHashes Common;  // 残す行のうち、前回そのファイルを読んだ TU すべてが出した行
  bool Exact = true;  // 前回の行の出どころが分かった
};

// 前回の索引と比べて、解析し直す TU(Todo)と残す行(K)を決める。
// 解析し直すのは、新しい TU・前回失敗した TU・コマンドの変わった TU・前回の後に変わった(消えた)ファイルを読む TU。
// 前回の行を捨てるのは、変わったファイルと、解析し直す TU か消えた TU だけが読んでいたファイルの行。
// それ以外のファイル(残す TU も読むヘッダーなど)は、残す TU のどれかが前回出した行(.own.bin で分かる)だけを残して、
// 新しい行を足す。.own.bin が無い古い索引では、前回の行をすべて残す。
bool planUpdate(const tooling::CompilationDatabase &Cdb, const std::vector<std::string> &All, const std::string &RootN,
                std::vector<std::string> &Todo, Kept &K) {
  llvm::sys::fs::file_status St;
  if (llvm::sys::fs::status(Out, St) || !llvm::sys::fs::exists(Out + ".deps.tsv")) {
    llvm::errs() << "-update: 前回の索引(" << Out << " と .deps.tsv)がありません。-update を付けずに作り直してください\n";
    return false;
  }
  auto Prev = St.getLastModificationTime();
  struct OldTu {
    std::string Path, Line;
    bool Ok = false;
    std::string Hash;
    std::vector<std::string> Deps;
  };
  std::map<std::string, OldTu> Old;
  std::vector<std::string> Order, Lines;
  OwnFile Own;
  if (std::string Why = readOwn(Out + ".own.bin", fileHash(Out + ".tu.tsv"), Own); !Why.empty() || !Own.Exact) {
    llvm::errs() << "update: 行の出どころ(" << Out << ".own.bin)が" << (Why.empty() ? "不正確" : Why)
                 << "なので、複数の TU が読むファイルの前回の行はすべて残します(古い行が残りえます)。"
                    "-update を付けずに作り直すと直ります\n";
    K.Exact = false;
    Own.Tus.clear();
  }
  readLines(Out + ".tu.tsv", Lines, true);
  for (const auto &L : Lines) {
    llvm::SmallVector<llvm::StringRef, 8> C;
    llvm::StringRef(L).split(C, '\t');
    if (C.size() < 2) continue;
    std::string Key = pathKey(C[0]);
    if (!Old.count(Key)) Order.push_back(Key);
    Old[Key] = {C[0].str(), L, C[1] == "1", C.size() > 6 ? C[6].str() : "", {}};
  }
  Lines.clear();
  readLines(Out + ".deps.tsv", Lines, true);
  for (const auto &L : Lines) {
    auto [T, F] = llvm::StringRef(L).split('\t');
    if (auto It = Old.find(pathKey(T)); It != Old.end()) It->second.Deps.push_back(F.str());
  }

  auto newer = [&](llvm::StringRef P) {
    llvm::sys::fs::file_status S;
    return llvm::sys::fs::status(P, S) || S.getLastModificationTime() > Prev;
  };
  std::map<std::string, bool> Changed;  // ルートからの相対パス -> 前回の後に変わったか消えた
  for (auto &[Key, T] : Old)
    for (const auto &D : T.Deps)
      if (!Changed.count(D)) {
        llvm::SmallString<260> P(RootN);
        llvm::sys::path::append(P, D);
        Changed[D] = newer(P);
      }
  std::set<std::string> Redo, InCdb;
  for (const auto &F : All) {
    std::string Key = pathKey(F);
    InCdb.insert(Key);
    auto It = Old.find(Key);
    bool R = It == Old.end() || !It->second.Ok || It->second.Hash != commandHash(Cdb, F) || newer(F);
    for (size_t J = 0; !R && J < It->second.Deps.size(); ++J) R = Changed[It->second.Deps[J]];
    if (R) {
      Todo.push_back(F);
      Redo.insert(Key);
    }
  }
  std::map<std::string, bool> OnlyRedone;  // そのファイルを読む TU が、すべて解析し直すか消えたもの
  for (auto &[Key, T] : Old) {
    bool Gone = Redo.count(Key) || !InCdb.count(Key);
    for (const auto &D : T.Deps) {
      auto [It, New] = OnlyRedone.try_emplace(D, Gone);
      if (!New) It->second = It->second && Gone;
    }
  }
  std::set<std::string> Drop;
  size_t NChanged = 0, Removed = 0;
  for (const auto &[D, C] : Changed) {
    NChanged += C;
    if (C || OnlyRedone[D]) Drop.insert(D);
  }
  // AllExtra: 前回の TU どれかの共通でない行。KeptExtra: そのうち残す TU のもの
  FileHashes AllExtra, KeptExtra;
  for (const auto &Key : Order) {
    OldTu &T = Old[Key];
    if (!InCdb.count(Key)) ++Removed;
    bool Keep = !Redo.count(Key) && InCdb.count(Key);
    FileHashes Extra;
    if (auto It = Own.Tus.find(Key); It != Own.Tus.end()) Extra = std::move(It->second);
    for (const auto &[F, V] : Extra) {
      AllExtra[F].insert(AllExtra[F].end(), V.begin(), V.end());
      if (Keep) KeptExtra[F].insert(KeptExtra[F].end(), V.begin(), V.end());
    }
    if (Keep) K.Tus.push_back({T.Path, T.Line, std::move(T.Deps), std::move(Extra)});
  }
  for (auto *M : {&AllExtra, &KeptExtra})
    for (auto &[F, V] : *M) sortUnique(V);
  static const std::vector<uint64_t> None;
  // 共通の行は、そのファイルを読む残す TU も出している。共通でない行は、残す TU のどれかが出したものだけを残す
  auto keep = [&](std::string &L, const std::string &Path, std::vector<std::string> &To) {
    if (Drop.count(Path)) return;
    uint64_t H = lineHash(L);
    auto It = AllExtra.find(Path);
    if (It != AllExtra.end() && std::binary_search(It->second.begin(), It->second.end(), H)) {
      auto KIt = KeptExtra.find(Path);
      const auto &KV = KIt == KeptExtra.end() ? None : KIt->second;
      if (!std::binary_search(KV.begin(), KV.end(), H)) return;
    } else {
      K.Common[Path].push_back(H);
    }
    To.push_back(std::move(L));
  };
  Lines.clear();
  readLines(Out, Lines, false);
  for (auto &L : Lines) keep(L, rowPath(L).str(), K.Rows);
  Lines.clear();
  readLines(Out + ".skip.tsv", Lines, true);
  for (auto &L : Lines) keep(L, llvm::StringRef(L).split('\t').first.str(), K.Skips);
  for (auto &[F, V] : K.Common) sortUnique(V);
  llvm::errs() << "update: " << NChanged << " files changed, " << Todo.size() << "/" << All.size()
               << " TU to re-index, " << Removed << " TU removed\n";
  return true;
}

// <P>.tmp で <P> をすべて差し替える。どれかが差し替えられなければ何も変えない(新旧の混ざった索引を残さない)。
// 開かれているファイル(ltq が読んでいる最中など)は Windows では名前を変えられないので、まず古いほうを .old に
// 退かせる。ここで失敗したら退かせた分を戻す。退かせた後の .tmp -> 本体は、宛先が無いので開かれていても失敗しない
bool replaceAll(llvm::ArrayRef<std::string> Outs) {
  std::vector<std::string> Moved;
  auto rollback = [&] {
    for (const auto &P : Moved) llvm::sys::fs::rename(P + ".old", P);
  };
  for (const auto &P : Outs) {
    if (!llvm::sys::fs::exists(P)) continue;
    llvm::sys::fs::remove(P + ".old");
    // ltq の読み取りは 1 秒もかからないので、10 秒まで待つ(rename 自身も中で繰り返すので、回数でなく時間で区切る)
    auto Deadline = std::chrono::steady_clock::now() + std::chrono::seconds(10);
    std::error_code E;
    while ((E = llvm::sys::fs::rename(P, P + ".old")) && std::chrono::steady_clock::now() < Deadline)
      std::this_thread::sleep_for(std::chrono::milliseconds(100));
    if (E) {
      llvm::errs() << "cannot replace " << P << " (" << E.message() << "); the previous index is kept, new one is in *.tmp\n";
      rollback();
      return false;
    }
    Moved.push_back(P);
  }
  for (const auto &P : Outs)
    if (std::error_code E = llvm::sys::fs::rename(P + ".tmp", P)) {
      llvm::errs() << "rename " << P << ".tmp: " << E.message() << "\n";
      return false;
    }
  for (const auto &P : Moved) llvm::sys::fs::remove(P + ".old");  // 開かれていれば残るが、次の差し替えで上書きする
  return true;
}

}  // namespace

int main(int argc, const char **argv) {
  cl::ParseCommandLineOptions(argc, argv, "libtooling 版の C++ 参照索引(段階 1)\n");
  // 配布物(LLVM 無しで使う)は、組み込みヘッダーを ltidx.exe の隣の lib\clang\<版> に置く。あればビルドした LLVM の場所より優先する
  if (!ResourceDir.getNumOccurrences()) {
    llvm::SmallString<256> Bundled(llvm::sys::path::parent_path(
        llvm::sys::fs::getMainExecutable(argv[0], reinterpret_cast<void *>(&executableAnchor))));
    llvm::sys::path::append(Bundled, "lib", "clang", llvm::sys::path::filename(LTIDX_RESOURCE_DIR));
    if (llvm::sys::fs::is_directory(Bundled + "/include")) ResourceDir = std::string(Bundled);
  }
  if (!llvm::sys::fs::is_directory(ResourceDir + "/include")) {
    llvm::errs() << "clang の組み込みヘッダーがありません: " << ResourceDir
                 << "/include(-resource-dir で指定するか、ltidx.exe の隣の lib\\clang\\<版> に置いてください)\n";
    return 2;
  }
  llvm::sys::TimePoint<> Started = std::chrono::system_clock::now();
  std::string Err;
  auto Cdb = tooling::CompilationDatabase::loadFromDirectory(CdbPath, Err);
  if (!Cdb) { llvm::errs() << Err << "\n"; return 2; }
  if (Update && !Files.empty()) { llvm::errs() << "-update と対象ファイルは一緒に指定できません\n"; return 2; }

  // C/C++ 以外の入力(CMake の compile_commands.json に載るアセンブリ・リソース)と、モジュールの .ixx は解析しない。
  // 解析すると「失敗した TU」に数えられる
  std::vector<std::string> All;
  for (const auto &F : Cdb->getAllFiles()) {
    std::string Ext = llvm::sys::path::extension(F).lower();
    if (llvm::sys::fs::exists(F) && Ext != ".ixx" && Ext != ".asm" && Ext != ".rc" && Ext != ".s") All.push_back(F);
  }
  std::string RootN = normalize(Root);
  // ルートとファイルのパスで書き方が違うことがある(CI の一時フォルダ RUNNER~1 と、MSBuild が返す runneradmin など)
  std::vector<std::string> Roots{RootN};
  llvm::SmallString<260> Real;
  if (!llvm::sys::fs::real_path(RootN, Real)) Roots.push_back(normalize(Real));
  Roots.push_back(normalize(shortPath(RootN)));
  std::sort(Roots.begin(), Roots.end());
  Roots.erase(std::unique(Roots.begin(), Roots.end()), Roots.end());
  std::vector<std::string> Todo(Files.begin(), Files.end());
  Kept K;
  if (Update) {
    if (!planUpdate(*Cdb, All, RootN, Todo, K)) return 2;
  } else if (Todo.empty()) {
    Todo = All;
  }
  unsigned DefaultCp = SourceCp ? (unsigned)SourceCp : ansiCodepage();
  Transcoder Trans;
  if (!Overlay.empty() && !Trans.loadOverlay(Overlay)) {
    llvm::errs() << "overlay を読めない: " << Overlay << "\n";
    return 2;
  }

  // 書き終えてから差し替える(作っている途中の索引を ltq が読まないように)
  const std::string Outs[] = {Out, Out + ".tu.tsv", Out + ".skip.tsv", Out + ".deps.tsv", Out + ".own.bin"};
  std::error_code EC;
  llvm::raw_fd_ostream OutS(Outs[0] + ".tmp", EC), TuS(Outs[1] + ".tmp", EC), SkipS(Outs[2] + ".tmp", EC),
      DepS(Outs[3] + ".tmp", EC);
  if (EC) { llvm::errs() << EC.message() << "\n"; return 2; }
  TuS << "file\tok\terrors\tsec\trefs\tfirst_error\tcommand_hash\tsource_cp\n";
  SkipS << "path\tkind\tbegin\tend\n";
  DepS << "tu\tfile\n";

  std::mutex M;
  std::set<std::string> Seen, SeenSkip;
  for (auto &R : K.Rows)
    if (Seen.insert(R).second) OutS << R << "\n";
  for (auto &S : K.Skips)
    if (SeenSkip.insert(S).second) SkipS << S << "\n";
  for (auto &T : K.Tus) {
    TuS << T.Line << "\n";
    for (const auto &D : T.Deps) DepS << T.Path << "\t" << D << "\n";
  }
  K.Rows = {};
  K.Skips = {};

  // 解析した TU ごとの、読んだファイルと、出した行のハッシュ(.own.bin を作るのに使う)。
  // ヘッダーの多くは、どの TU からも同じ行になるので、同じハッシュの並びは 1 つを共有する(Pool。M で守る)
  using Hashes = std::shared_ptr<const std::vector<uint64_t>>;
  std::vector<std::vector<std::string>> NewDeps(Todo.size());
  std::vector<std::map<std::string, Hashes>> NewOwn(Todo.size());
  std::unordered_map<uint64_t, Hashes> Pool;
  std::atomic<size_t> Next{0}, Ok{0};
  auto Worker = [&] {
    for (size_t I; (I = Next++) < Todo.size();) {
      auto T0 = std::chrono::steady_clock::now();
      Output O;
      auto Cmds = Cdb->getCompileCommands(Todo[I]);
      unsigned Cp = Cmds.empty() ? DefaultCp : sourceCodepage(Cmds[0].CommandLine, DefaultCp);
      // 作業ディレクトリをプロセスと共有しないファイルシステム(TU ごとにコンパイルのディレクトリが違っても並列にできる)
      llvm::IntrusiveRefCntPtr<llvm::vfs::FileSystem> Base(llvm::vfs::createPhysicalFileSystem().release());
      tooling::ClangTool Tool(*Cdb, {Todo[I]}, std::make_shared<PCHContainerOperations>(),
                              llvm::makeIntrusiveRefCnt<TranscodingFS>(Base, Cp, Trans));
      Tool.appendArgumentsAdjuster([](const tooling::CommandLineArguments &A, llvm::StringRef) {
        tooling::CommandLineArguments R;
        for (const auto &S : A) {
          llvm::StringRef X(S);
          if (X.size() > 3 && (X[0] == '/' || X[0] == '-') &&
              (X.substr(1).starts_with("Yu") || X.substr(1).starts_with("Yc") || X.substr(1).starts_with("Fp")))
            continue;  // PCH は使わない(大文字小文字を区別して /fp:precise を残す)
          if (X == "/WX" || X == "-WX" || X == "-Werror" || X.starts_with("-Werror="))
            continue;  // cl に無い clang の警告(override の付け忘れなど)がエラーになり TU が落ちる
          if (isCharsetArg(X))
            continue;  // clang は UTF-8 以外を受け付けない。ソースは TranscodingFS が UTF-8 に直す
          R.push_back(S);
        }
        return R;
      });
      Tool.appendArgumentsAdjuster(tooling::getInsertArgumentAdjuster(
          ("-resource-dir=" + ResourceDir).c_str(), tooling::ArgumentInsertPosition::BEGIN));
      // 遅延解析のままだと、実体化されないテンプレート本体の非依存呼び出しが索引に載らない。MSVC 向けでは
      // gcc 形式(g++・clang++)の引数でも既定で遅延するので、どちらの形式でも止める
      if (!Delayed)
        Tool.appendArgumentsAdjuster([](const tooling::CommandLineArguments &A, llvm::StringRef File) {
          return tooling::getInsertArgumentAdjuster(isClDriver(A) ? "/Zc:twoPhase" : "-fno-delayed-template-parsing",
                                                    tooling::ArgumentInsertPosition::END)(A, File);
        });
      ErrorCounter Diag;
      Tool.setDiagnosticConsumer(&Diag);
      Factory F(Roots, O);
      int Rc = Tool.run(&F);
      double Sec = std::chrono::duration<double>(std::chrono::steady_clock::now() - T0).count();
      bool Good = Rc == 0 && Diag.Errors == 0;
      if (Good) ++Ok;
      std::string Hash = commandHash(*Cdb, Todo[I]);
      std::set<std::string> Deps(O.Deps.begin(), O.Deps.end());
      FileHashes Own;
      std::vector<uint64_t> *Last = nullptr;
      llvm::StringRef LastPath;
      auto own = [&](const std::string &L, llvm::StringRef Path) {
        if (!Last || Path != LastPath) { Last = &Own[Path.str()]; LastPath = Path; }
        Last->push_back(lineHash(L));
      };
      for (const auto &R : O.Rows) own(R, rowPath(R));
      for (const auto &S : O.Skips) own(S, llvm::StringRef(S).split('\t').first);
      for (auto &[F, V] : Own) sortUnique(V);

      std::lock_guard<std::mutex> L(M);
      for (auto &[F, V] : Own) {
        uint64_t Key = llvm::xxh3_64bits(llvm::ArrayRef<uint8_t>(reinterpret_cast<const uint8_t *>(V.data()), V.size() * 8));
        auto It = Pool.find(Key);
        if (It != Pool.end() && *It->second == V) {
          NewOwn[I][F] = It->second;
        } else {  // 初めての並び(ハッシュが衝突したものは共有しない)
          auto P = std::make_shared<const std::vector<uint64_t>>(std::move(V));
          if (It == Pool.end()) Pool.emplace(Key, P);
          NewOwn[I][F] = std::move(P);
        }
      }
      size_t New = 0;
      for (auto &R : O.Rows)
        if (Seen.insert(R).second) { OutS << R << "\n"; ++New; }
      for (auto &S : O.Skips)
        if (SeenSkip.insert(S).second) SkipS << S << "\n";
      for (const auto &D : Deps) DepS << Todo[I] << "\t" << D << "\n";
      NewDeps[I].assign(Deps.begin(), Deps.end());
      TuS << Todo[I] << "\t" << (Good ? 1 : 0) << "\t" << Diag.Errors << "\t" << llvm::format("%.2f", Sec) << "\t"
          << New << "\t" << Diag.First << "\t" << Hash << "\t" << Cp << "\n";
      llvm::errs() << "[" << (I + 1) << "/" << Todo.size() << "] " << (Good ? "ok " : "NG ")
                   << llvm::sys::path::filename(Todo[I]) << "\n";
    }
  };
  // 深いテンプレートの実体化で、clang の解析は 1 MB(Windows のスレッドの既定)のスタックを使い切る(O3DE で
  // STATUS_STACK_OVERFLOW になった)。clang 本体の 8 MB より余裕を持たせる
  constexpr unsigned WorkerStack = 16u << 20;
  std::vector<llvm::thread> Ts;
  for (unsigned J = 0; J < std::max(1u, (unsigned)Jobs); ++J) Ts.emplace_back(std::optional<unsigned>(WorkerStack), Worker);
  for (auto &T : Ts) T.join();

  for (auto *S : {&OutS, &TuS, &SkipS, &DepS}) {
    S->close();
    if (S->has_error()) { llvm::errs() << "write failed: " << S->error().message() << "\n"; return 2; }
  }

  // 行の出どころ。複数の TU が読むファイルごとに、読む TU すべてが出した行(共通)を求め、TU ごとに共通でない行を残す。
  // 残す TU が前回出した行は、前回の共通の行(K.Common)と前回の共通でない行(KeptTu::Extra)を合わせたもの
  {
    size_t NK = K.Tus.size();
    std::map<std::string, std::vector<size_t>> Readers;  // ファイル -> 読む TU(残す TU は 0..NK-1、解析した TU は NK+I)
    for (size_t I = 0; I < NK; ++I)
      for (const auto &D : K.Tus[I].Deps) Readers[D].push_back(I);
    for (size_t I = 0; I < Todo.size(); ++I)
      for (const auto &D : NewDeps[I]) Readers[D].push_back(NK + I);
    static const std::vector<uint64_t> None;
    auto get = [&](const FileHashes &M, const std::string &F) -> const std::vector<uint64_t> & {
      auto It = M.find(F);
      return It == M.end() ? None : It->second;
    };
    auto getNew = [&](size_t I, const std::string &F) -> const std::vector<uint64_t> & {
      auto It = NewOwn[I].find(F);
      return It == NewOwn[I].end() ? None : *It->second;
    };
    std::vector<FileHashes> Extra(NK + Todo.size());
    for (const auto &[F, R] : Readers) {
      if (R.size() < 2) continue;
      const auto &Common = get(K.Common, F);
      size_t NKept = std::count_if(R.begin(), R.end(), [&](size_t I) { return I < NK; });
      std::unordered_map<uint64_t, size_t> Count;  // 行 -> 出した TU の数
      if (NKept)
        for (uint64_t H : Common) Count[H] += NKept;
      for (size_t I : R)
        for (uint64_t H : I < NK ? get(K.Tus[I].Extra, F) : getNew(I - NK, F)) ++Count[H];
      for (size_t I : R) {
        std::vector<uint64_t> E;
        auto add = [&](const std::vector<uint64_t> &V) {
          for (uint64_t H : V)
            if (Count[H] < R.size()) E.push_back(H);
        };
        if (I < NK) {
          add(Common);
          add(get(K.Tus[I].Extra, F));
          sortUnique(E);
        } else {
          add(getNew(I - NK, F));
        }
        if (!E.empty()) Extra[I][F] = std::move(E);
      }
    }
    std::vector<std::pair<std::string, const FileHashes *>> Tus;
    for (size_t I = 0; I < Extra.size(); ++I)
      if (!Extra[I].empty()) Tus.push_back({I < NK ? K.Tus[I].Path : Todo[I - NK], &Extra[I]});
    if (!writeOwn(Outs[4] + ".tmp", fileHash(Outs[1] + ".tmp"), !Update || K.Exact || K.Tus.empty(), Tus)) return 2;
  }
  // 索引の時刻は解析を始めた時刻にする。解析の途中で変わったファイルも、次の -update と ltq が「索引の後に変わった」と扱う
  for (const auto &P : Outs) llvm::sys::fs::setLastAccessAndModificationTime(P + ".tmp", Started);
  if (!replaceAll(Outs)) return 2;
  llvm::errs() << "TU " << Ok << "/" << Todo.size() << " ok, rows " << Seen.size() << "\n";
  return Ok == Todo.size() ? 0 : 1;
}
