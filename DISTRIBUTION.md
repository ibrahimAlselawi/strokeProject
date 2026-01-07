# How to Distribute This Project

Because your project has large files (`.gml` ~400MB), you cannot simply upload it to GitHub like normal. We have set up **Git LFS** (Large File Storage) and **GitHub Actions** to handle this for you.

## 1. Install Git LFS

You must install Git Large File Storage on your computer once.

1.  Download and install Git LFS from: https://git-lfs.com
2.  Open your terminal (Command Prompt or PowerShell).
3.  Run: `git lfs install`

## 2. Push to GitHub

Now you can push your project to GitHub. The large files will be handled automatically.

1.  Create a new repository on GitHub.
2.  Run these commands in your project folder:
    ```bash
    git init
    git add .
    git commit -m "Initial commit with distribution setup"
    git branch -M main
    git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO_NAME.git
    git push -u origin main
    ```

## 3. Get the Apps

Once you push, GitHub will automatically build the Windows and Mac apps for you!

1.  Go to your GitHub repository page.
2.  Click on the **Actions** tab at the top.
3.  Click on the latest workflow run (e.g., "Initial commit...").
4.  Scroll down to the **Artifacts** section.
5.  You will see `stroke-access-windows-latest` and `stroke-access-macos-latest`.
6.  Download these zip files. Inside are the `.exe` (Windows) and binary (Mac) files.

## 4. Send to Friends

- **Windows Users**: Send them the `.exe` file. They just double-click it.
- **Mac Users**: Send them the Mac file.
    - *Note*: Since we are not paying Apple $99/year to sign the app, your friends might see a security warning. They should right-click the app and choose "Open", then click "Open" in the dialog.

## Troubleshooting

- **"File too large" error**: Did you install Git LFS? Run `git lfs install` and try again.
- **App closes immediately**: The app runs in a console window. If it finishes or crashes, the window closes. You might want to run it from a terminal to see errors, or trust the `requirements.txt` included all dependencies.
