import { z } from "zod";

// The register form's schema lives apart from lib/validation so that the search wizard,
// which imports only plain helpers from there, does not bundle zod.
export const registerSchema = z
  .object({
    full_name: z
      .string()
      .trim()
      .min(1, { message: "Enter your name." })
      .max(120)
      .refine((v) => !/[<>]/.test(v), { message: "Name must not contain < or >." }),
    email: z.email({ message: "Enter a valid email address." }),
    password: z
      .string()
      .min(10, { message: "Use at least 10 characters." })
      .max(128)
      .refine((v) => /[A-Za-z]/.test(v) && /[^A-Za-z]/.test(v), {
        message: "Include letters and at least one number or symbol.",
      }),
    confirm_password: z.string(),
  })
  .refine((v) => v.password === v.confirm_password, { message: "Passwords do not match.", path: ["confirm_password"] });

export type RegisterValues = z.infer<typeof registerSchema>;
