// Copyright 2020 The Chromium Authors. All rights reserved.
// Use of this source code is governed by a BSD-style license that can be
// found in the LICENSE file.

// This is implementation of a clang tool that rewrites raw pointer fields into
// CheckedPtr<T>:
//     Pointee* field_
// becomes:
//     CheckedPtr<Pointee> field_
//
// For more details, see the doc here:
// https://docs.google.com/document/d/1chTvr3fSofQNV_PDPEHRyUgcJCQBgTDOOBriW9gIm9M

#include <assert.h>
#include <algorithm>
#include <memory>
#include <string>
#include <vector>

#include "clang/AST/ASTContext.h"
#include "clang/ASTMatchers/ASTMatchFinder.h"
#include "clang/ASTMatchers/ASTMatchers.h"
#include "clang/ASTMatchers/ASTMatchersMacros.h"
#include "clang/Basic/CharInfo.h"
#include "clang/Basic/SourceManager.h"
#include "clang/Frontend/CompilerInstance.h"
#include "clang/Frontend/FrontendActions.h"
#include "clang/Lex/Lexer.h"
#include "clang/Lex/MacroArgs.h"
#include "clang/Lex/PPCallbacks.h"
#include "clang/Lex/Preprocessor.h"
#include "clang/Tooling/CommonOptionsParser.h"
#include "clang/Tooling/Refactoring.h"
#include "clang/Tooling/Tooling.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/ErrorOr.h"
#include "llvm/Support/LineIterator.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/Path.h"
#include "llvm/Support/TargetSelect.h"

using namespace clang::ast_matchers;
using clang::tooling::CommonOptionsParser;
using clang::tooling::Replacement;

namespace {

class FieldDeclRewriter : public MatchFinder::MatchCallback {
 public:
  explicit FieldDeclRewriter(std::vector<Replacement>* replacements)
      : replacements_(replacements) {}

  void run(const MatchFinder::MatchResult& result) override {
    const clang::SourceManager& source_manager = *result.SourceManager;
    const clang::FieldDecl* field_decl =
        result.Nodes.getNodeAs<clang::FieldDecl>("fieldDecl");
    const clang::TypeSourceInfo* type_source_info =
        field_decl->getTypeSourceInfo();

    clang::QualType pointer_type = type_source_info->getType();
    assert(type_source_info->getType()->isPointerType() &&
           "matcher should only match pointer types");

    // Calculate the |replacement_range|.
    //
    // Consider the following example:
    //      const Pointee* const field_name_;
    //      ^-------------------^   = |replacement_range|
    //                           ^  = |field_decl->getLocation()|
    //      ^                       = |field_decl->getBeginLoc()|
    //                   ^          = PointerTypeLoc::getStarLoc
    //            ^------^          = TypeLoc::getSourceRange
    //
    // We get the |replacement_range| in a bit clumsy way, because clang docs
    // for QualifiedTypeLoc explicitly say that these objects "intentionally do
    // not provide source location for type qualifiers".
    clang::SourceRange replacement_range(
        field_decl->getBeginLoc(),
        field_decl->getLocation().getLocWithOffset(-1));

    // Generate and add a replacement.
    replacements_->emplace_back(
        source_manager, clang::CharSourceRange::getCharRange(replacement_range),
        GenerateNewText(pointer_type));
  }

 private:
  std::string GenerateNewText(const clang::QualType& pointer_type) {
    assert(pointer_type->isPointerType() && "caller must pass a pointer type!");
    clang::QualType pointee_type = pointer_type->getPointeeType();

    // Convert pointee type to string.
    clang::LangOptions lang_options;
    clang::PrintingPolicy printing_policy(lang_options);
    printing_policy.SuppressTagKeyword = 1;  // s/class Pointee/Pointee/
    std::string pointee_type_as_string =
        pointee_type.getAsString(printing_policy);

    // TODO(lukasza): Preserve qualifiers from |pointer_type| by generating
    // results from fresh AST (rather than via string concatenation).
    return std::string("CheckedPtr<") + pointee_type_as_string + ">";
  }

  std::vector<Replacement>* const replacements_;
};

}  // namespace

int main(int argc, const char* argv[]) {
  // TODO(dcheng): Clang tooling should do this itself.
  // http://llvm.org/bugs/show_bug.cgi?id=21627
  llvm::InitializeNativeTarget();
  llvm::InitializeNativeTargetAsmParser();
  llvm::cl::OptionCategory category(
      "rewrite_raw_ptr_fields: changes |T* field_| to |CheckedPtr<T> field_|.");
  CommonOptionsParser options(argc, argv, category);
  clang::tooling::ClangTool tool(options.getCompilations(),
                                 options.getSourcePathList());

  MatchFinder match_finder;
  std::vector<Replacement> replacements;

  // Field declarations =========
  // Given
  //   struct S {
  //     int* y;
  //   };
  // matches |int* y|.
  auto field_decl_matcher = fieldDecl(hasType(pointerType())).bind("fieldDecl");
  FieldDeclRewriter field_decl_rewriter(&replacements);
  match_finder.addMatcher(field_decl_matcher, &field_decl_rewriter);

  // Prepare and run the tool.
  std::unique_ptr<clang::tooling::FrontendActionFactory> factory =
      clang::tooling::newFrontendActionFactory(&match_finder);
  int result = tool.run(factory.get());
  if (result != 0)
    return result;

  // Serialization format is documented in tools/clang/scripts/run_tool.py
  llvm::outs() << "==== BEGIN EDITS ====\n";
  for (const auto& r : replacements) {
    std::string replacement_text = r.getReplacementText().str();
    std::replace(replacement_text.begin(), replacement_text.end(), '\n', '\0');
    llvm::outs() << "r:::" << r.getFilePath() << ":::" << r.getOffset()
                 << ":::" << r.getLength() << ":::" << replacement_text << "\n";
  }
  llvm::outs() << "==== END EDITS ====\n";

  return 0;
}
